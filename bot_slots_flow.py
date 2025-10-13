import os
import sys
from typing import Optional
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from decimal import Decimal
import telegram, ROUND_DOWN, InvalidOperation
from typing import Dict, List, Tuple, Optional
from pathlib import Path

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ==== ENV (.env esplicito) ====
try:
    from dotenv import load_dotenv
    DOTENV_PATH = Path(__file__).with_name(".env")
    print("DEBUG .env path:", DOTENV_PATH, "exists:", DOTENV_PATH.exists())
    load_dotenv(dotenv_path=DOTENV_PATH)
except Exception as e:
    print("DEBUG dotenv load error:", e)

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
_raw_admin_ids = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS: List[int] = [int(x) for x in _raw_admin_ids.split(",") if x.strip().isdigit()]
if not ADMIN_IDS:
    _single = os.getenv("ADMIN_ID", "").strip()
    if _single.isdigit():
        ADMIN_IDS = [int(_single)]

print("DEBUG TOKEN:", "OK" if TOKEN else "MANCANTE")
print("DEBUG ADMIN_IDS:", ADMIN_IDS)

DB_PATH = "kwh_slots.db"
ALLOW_NEGATIVE = os.getenv("ALLOW_NEGATIVE", "1").strip() in {"1","true","True","YES","yes"}

SLOTS = (8, 3, 5)

# ==== DECIMAL ====
QK = Decimal("0.0001")
def qk(x: Decimal) -> Decimal:
    return x.quantize(QK, rounding=ROUND_DOWN)

def parse_kwh_positive(s: str) -> Decimal:
    try:
        d = qk(Decimal(s.strip().replace(",", ".")))
        if d <= 0:
            raise ValueError
        return d
    except (InvalidOperation, ValueError):
        raise

def parse_kwh_any(s: str) -> Decimal:
    return qk(Decimal(s.strip().replace(",", ".")))

def fmt_kwh(d: Decimal) -> str:
    s = f"{qk(d):f}"
    if "." in s:
        i, dec = s.split(".", 1)
        dec = dec[:4].rstrip("0")
        return i if dec == "" else f"{i}.{dec}"
    return s

# ==== DB ====
from sqlite3 import OperationalError

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            balance_slot8 REAL DEFAULT 0,
            balance_slot3 REAL DEFAULT 0,
            balance_slot5 REAL DEFAULT 0,
            approved INTEGER DEFAULT 0,
            created_at TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS recharges(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            slot INTEGER NOT NULL CHECK(slot IN (8,3,5)),
            kwh REAL NOT NULL,
            photo_file_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewer_id INTEGER,
            note TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recharge_id INTEGER NOT NULL,
            admin_chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            FOREIGN KEY(recharge_id) REFERENCES recharges(id) ON DELETE CASCADE
        )
        """)

        # Migrazioni idempotenti
        for col in ("balance_slot8", "balance_slot3", "balance_slot5"):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} REAL DEFAULT 0")
            except OperationalError:
                pass
        for alter in (
            "ALTER TABLE recharges ADD COLUMN slot INTEGER NOT NULL DEFAULT 8",
            "ALTER TABLE recharges ADD COLUMN note TEXT",
        ):
            try:
                conn.execute(alter)
            except OperationalError:
                pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN approved INTEGER DEFAULT 0")
        except OperationalError:
            pass
        # created_at su users
        try:
            conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT")
        except OperationalError:
            pass
        # Valorizza created_at mancante
        conn.execute("""
            UPDATE users
            SET created_at = COALESCE(created_at, ?)
            WHERE created_at IS NULL
        """, (datetime.utcnow().isoformat(),))

# ==== DB helpers ====
def ensure_user(u):
    with db() as conn:
        conn.execute("""
        INSERT OR IGNORE INTO users(user_id, username, first_name, last_name, created_at)
        VALUES(?,?,?,?,?)
        """, (u.id, u.username or "", u.first_name or "", u.last_name or "", datetime.utcnow().isoformat()))
        conn.execute("""
        UPDATE users SET username=?, first_name=?, last_name=?
        WHERE user_id=?
        """, (u.username or "", u.first_name or "", u.last_name or "", u.id))

def get_balances(user_id: int) -> Dict[int, Decimal]:
    with db() as conn:
        cur = conn.execute("SELECT balance_slot8,balance_slot3,balance_slot5 FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row:
            return {8:qk(Decimal("0")),3:qk(Decimal("0")),5:qk(Decimal("0"))}
        return {
            8: qk(Decimal(str(row[0] or 0))),
            3: qk(Decimal(str(row[1] or 0))),
            5: qk(Decimal(str(row[2] or 0)))
        }

def set_balance_slot(user_id:int, slot:int, new_bal:Decimal):
    col = {8:"balance_slot8",3:"balance_slot3",5:"balance_slot5"}[slot]
    with db() as conn:
        conn.execute(f"UPDATE users SET {col}=? WHERE user_id=?", (float(qk(new_bal)), user_id))

def add_balance_slot(user_id:int, slot:int, delta:Decimal):
    b = get_balances(user_id)
    set_balance_slot(user_id, slot, b[slot] + qk(delta))

def create_recharge(user_id:int, slot:int, kwh:Decimal, photo_file_id:str, note:str|None=None) -> int:
    with db() as conn:
        cur = conn.execute("""
        INSERT INTO recharges(user_id,slot,kwh,photo_file_id,status,created_at,note)
        VALUES(?,?,?,?, 'pending', ?, ?)
        """, (user_id, slot, float(qk(kwh)), photo_file_id, datetime.utcnow().isoformat(), note or ""))
        return cur.lastrowid

def get_recharge(rid:int):
    with db() as conn:
        cur = conn.execute("SELECT id,user_id,slot,kwh,photo_file_id,status,note FROM recharges WHERE id=?", (rid,))
        return cur.fetchone()

def set_recharge_status(rid:int, status:str, reviewer_id:int, note:str|None=None):
    with db() as conn:
        conn.execute("""
        UPDATE recharges SET status=?, reviewed_at=?, reviewer_id=?, note=COALESCE(note,'')
        WHERE id=?
        """, (status, datetime.utcnow().isoformat(), reviewer_id, rid))
        if note:
            conn.execute("UPDATE recharges SET note=? WHERE id=?", (note, rid))

def save_admin_notification(recharge_id:int, admin_chat_id:int, message_id:int):
    with db() as conn:
        conn.execute("""
        INSERT INTO admin_notifications(recharge_id,admin_chat_id,message_id)
        VALUES(?,?,?)
        """, (recharge_id, admin_chat_id, message_id))

def get_admin_notifications(recharge_id:int) -> List[Tuple[int,int]]:
    with db() as conn:
        cur = conn.execute("""
        SELECT admin_chat_id, message_id
        FROM admin_notifications
        WHERE recharge_id=?
        """, (recharge_id,))
        return cur.fetchall()

def sum_pending_for(user_id:int, slot:int) -> Decimal:
    with db() as conn:
        cur = conn.execute("""
        SELECT COALESCE(SUM(kwh),0) FROM recharges
        WHERE user_id=? AND slot=? AND status='pending'
        """, (user_id, slot))
        s = cur.fetchone()[0] or 0
        return qk(Decimal(str(s)))

# ==== LISTA UTENTI (con data iscrizione e ricerca) ====
def list_users(filter_status: str = "approved", limit: int = 20, offset: int = 0, search: Optional[str] = None):
    where_clauses = []
    params: List = []

    if filter_status == "approved":
        where_clauses.append("approved = 1")
    elif filter_status == "pending":
        where_clauses.append("approved = 0")
    # "tutti" -> nessun vincolo su approved

    if search:
        # cerca su username, first_name, last_name
        where_clauses.append("(COALESCE(username,'') LIKE ? OR COALESCE(first_name,'') LIKE ? OR COALESCE(last_name,'') LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])

    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    query = f"""
        SELECT user_id, username, first_name, last_name,
               COALESCE(balance_slot8,0), COALESCE(balance_slot3,0), COALESCE(balance_slot5,0),
               COALESCE(approved,0), COALESCE(created_at,'')
        FROM users
        {where}
        ORDER BY user_id ASC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    with db() as conn:
        cur = conn.execute(query, tuple(params))
        return cur.fetchall()

def count_users(filter_status: str = "approved", search: Optional[str] = None) -> int:
    where_clauses = []
    params: List = []
    if filter_status == "approved":
        where_clauses.append("approved = 1")
    elif filter_status == "pending":
        where_clauses.append("approved = 0")
    if search:
        where_clauses.append("(COALESCE(username,'') LIKE ? OR COALESCE(first_name,'') LIKE ? OR COALESCE(last_name,'') LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])

    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    with db() as conn:
        cur = conn.execute(f"SELECT COUNT(*) FROM users {where}", tuple(params))
        return int(cur.fetchone()[0] or 0)

def pending_count_for_user(user_id: int) -> int:
    with db() as conn:
        cur = conn.execute(
            "SELECT COUNT(*) FROM recharges WHERE user_id=? AND status='pending'",
            (user_id,)
        )
        return int(cur.fetchone()[0] or 0)

# ==== HELPERS ====
def is_admin(uid:int) -> bool:
    return uid in ADMIN_IDS

def is_user_approved(user_id:int) -> bool:
    with db() as conn:
        cur = conn.execute("SELECT approved FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return bool(row and (row[0] or 0) == 1)

async def guard_approved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = (update.effective_user.id if update.effective_user else None)
    if not uid:
        return False
    if is_user_approved(uid):
        return True
    if update.message:
        await update.message.reply_text("Il tuo account è in attesa di approvazione da parte di un amministratore.")
    elif update.callback_query:
        await update.callback_query.answer("Account in attesa di approvazione.", show_alert=True)
    return False

def main_keyboard(user_id: Optional[int] = None):
    return ReplyKeyboardMarkup(
        [[KeyboardButton("➕ Ricarica")],
         [KeyboardButton("/saldo"), KeyboardButton("/annulla")]],
        resize_keyboard=True
    )


def slots_keyboard(user_id: Optional[int] = None):
    try:
        b = get_balances(user_id) if user_id is not None else None
    except Exception:
        b = None
    def label(slot):
        if b and slot in b:
            return f"Slot {slot} • {fmt_kwh(b[slot])} kWh"
        return f"Slot {slot}"
        # dynamic wallet label
    wlabel = "💳 Ricarica wallet"
    if user_id is not None:
        try:
            w = fmt_kwh(get_wallet_kwh(user_id))
            wlabel = f"💳 Wallet • {w} kWh"
        except Exception:
            pass
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label(8), callback_data="slot:8")],
        [InlineKeyboardButton(label(3), callback_data="slot:3")],
        [InlineKeyboardButton(label(5), callback_data="slot:5")],
        [InlineKeyboardButton("Annulla", callback_data="flow:cancel")],
        [InlineKeyboardButton(wlabel, callback_data="wallet:req")]
    ])


def post_photo_keyboard():
        # dynamic wallet label
    wlabel = "💳 Ricarica wallet"
    if user_id is not None:
        try:
            w = fmt_kwh(get_wallet_kwh(user_id))
            wlabel = f"💳 Wallet • {w} kWh"
        except Exception:
            pass
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Aggiungi nota", callback_data="flow:add_note")],
        [InlineKeyboardButton("Conferma ricarica", callback_data="flow:confirm")],
        [InlineKeyboardButton("Annulla", callback_data="flow:cancel")],
        [InlineKeyboardButton(wlabel, callback_data="wallet:req")]
    ])

# ==== APPROVAZIONE UTENTI ====
async def on_user_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Solo gli amministratori possono approvare utenti.", show_alert=True)
        return
    try:
        _, action, uid_s = q.data.split(":")
        uid = int(uid_s)
    except Exception:
        try:
            await q.edit_message_text("Callback non valida.")
        except:
            await context.bot.send_message(chat_id=q.message.chat.id, text="Callback non valida.")
        return

    if action == "approve":
        with db() as conn:
            conn.execute("UPDATE users SET approved=1 WHERE user_id=?", (uid,))
        try:
            await q.edit_message_text(f"Utente {uid} approvato.")
        except:
            await context.bot.send_message(chat_id=q.message.chat.id, text=f"Utente {uid} approvato.")
        try:
            await context.bot.send_message(chat_id=uid, text="Il tuo account è stato approvato. Puoi usare il bot.")
        except:
            pass
    elif action == "reject":
        with db() as conn:
            conn.execute("UPDATE users SET approved=0 WHERE user_id=?", (uid,))
        try:
            await q.edit_message_text(f"Utente {uid} rifiutato.")
        except:
            await context.bot.send_message(chat_id=q.message.chat.id, text=f"Utente {uid} rifiutato.")
        try:
            await context.bot.send_message(chat_id=uid, text="La tua richiesta è stata rifiutata.")
        except:
            pass

# ==== HANDLERS UTENTE ====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u)

    if is_user_approved(u.id):
        await update.message.reply_text(
            "Benvenuto. In basso trovi i pulsanti rapidi.\n"
            "• Premi “➕ Ricarica” per iniziare.\n"
            "• /saldo per vedere i saldi (Slot 8/3/5).\n"
            "• /annulla per annullare il flusso in corso.",
            reply_markup=main_keyboard(update.effective_user.id)
        )
        return

    await update.message.reply_text("Richiesta inviata. Il tuo account è in attesa di approvazione da parte di un amministratore.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Approva utente", callback_data=f"user:approve:{u.id}")],
        [InlineKeyboardButton("Rifiuta utente",  callback_data=f"user:reject:{u.id}")]
    ])
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=aid,
                text=(f"Nuovo utente in attesa di approvazione:\n"
                      f"- ID: {u.id}\n- Username: @{u.username or '-'}\n- Nome: {u.first_name or ''} {u.last_name or ''}"),
                reply_markup=kb
            )
        except:
            pass


async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin: allow /saldo <user_identifier>
    if update.effective_user and is_admin(update.effective_user.id):
        parts = (update.message.text or "").strip().split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() == "/saldo":
            target = resolve_user_identifier(parts[1])
            if target is None:
                await update.message.reply_text("Utente non trovato.")
                return
            if isinstance(target, list):
                lines = ["Trovo più utenti, scegli l'ID:"]
                for uid, username, fn, ln in target:
                    name = (fn or "") + (" " + ln if ln else "")
                    who = f"@{username}" if username else (name.strip() or str(uid))
                    lines.append(f"• {who} (id {uid})")
                await update.message.reply_text("\n".join(lines))
                return
            other_id = int(target)
            b = get_balances(other_id)
            wallet = get_wallet_kwh(other_id)
            await update.message.reply_text(
                "Saldi utente {oid}:\n"
                f"• Slot 8: {fmt_kwh(b[8])} kWh\n"
                f"• Slot 3: {fmt_kwh(b[3])} kWh\n"
                f"• Slot 5: {fmt_kwh(b[5])} kWh\n"
                f"• Wallet: {fmt_kwh(wallet)} kWh"
                .format(oid=other_id),
                reply_markup=main_keyboard(update.effective_user.id)
            )
            return
    
# Admin: /saldo <user_id>
if update.effective_user and is_admin(update.effective_user.id):
    parts = (update.message.text or "").strip().split()
    if len(parts) == 2 and parts[0].lower() == "/saldo":
        try:
            other_id = int(parts[1])
            b = get_balances(other_id)
            await update.message.reply_text(
                "Saldi utente {oid}:\n"
                f"• Slot 8: {fmt_kwh(b[8])} kWh\n"
                f"• Slot 3: {fmt_kwh(b[3])} kWh\n"
                f"• Slot 5: {fmt_kwh(b[5])} kWh\n" + f"• Wallet: {fmt_kwh(get_wallet_kwh(u.id))} kWh\n"
                .format(oid=other_id),
                reply_markup=main_keyboard(update.effective_user.id)
            )
            return
        except Exception:
            pass

    if not await guard_approved(update, context):
        return
    u = update.effective_user
    ensure_user(u)
    b = get_balances(u.id)
    await update.message.reply_text(
        "Saldi attuali:\n"
        f"• Slot 8: {fmt_kwh(b[8])} kWh\n"
        f"• Slot 3: {fmt_kwh(b[3])} kWh\n"
        f"• Slot 5: {fmt_kwh(b[5])} kWh\n" + f"• Wallet: {fmt_kwh(get_wallet_kwh(u.id))} kWh\n",
        reply_markup=main_keyboard(update.effective_user.id)
    )

async def annulla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Operazione annullata. Nessuna richiesta in sospeso.", reply_markup=main_keyboard(update.effective_user.id))

# ENTRY: pulsante “➕ Ricarica”
async def on_ricarica_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_approved(update, context):
        return
    if (update.message.text or "") != "➕ Ricarica":
        return
    context.user_data.clear()
    context.user_data["pending_step"] = "choose_slot"
    await update.message.reply_text("Scegli lo slot da cui scalare la ricarica:", reply_markup=main_keyboard(update.effective_user.id))
    await update.message.reply_text("Slot disponibili:", reply_markup=slots_keyboard(update.effective_user.id))

# Scelta slot
async def on_slot_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await guard_approved(update, context):
        return
    data = q.data
    if data == "flow:cancel":
        context.user_data.clear()
        await q.edit_message_text("Operazione annullata.")
        return
    if not data.startswith("slot:"):
        return

    if context.user_data.get("pending_step") not in (None, "choose_slot", "enter_kwh"):
        await q.answer("Sequenza non valida.", show_alert=True)
        return

    slot = int(data.split(":")[1])
    if slot not in SLOTS:
        await q.answer("Slot non valido.", show_alert=True)
        return

    context.user_data["pending_slot"] = slot
    context.user_data["pending_step"] = "enter_kwh"

    try:
        await q.edit_message_text(f"Slot selezionato: {slot}")
    except:
        pass
    await q.message.chat.send_message("Inserisci i kWh che vuoi dichiarare (es. 12.3456).")

# Handler unificato per input testuale (kWh o nota)
async def on_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_approved(update, context):
        return

    step = context.user_data.get("pending_step")

    # Inserimento kWh
    if step == "enter_kwh":
        try:
            kwh = parse_kwh_positive(update.message.text)
        except Exception:
            await update.message.reply_text("Valore non valido. Scrivi i kWh (es. 12.3456).")
            return

        context.user_data["pending_kwh"] = str(kwh)
        context.user_data["pending_step"] = "await_photo"

        slot = context.user_data.get("pending_slot")
        bals = get_balances(update.effective_user.id)
        after_only_this = bals[slot] - kwh
        pending_tot = sum_pending_for(update.effective_user.id, slot) + kwh
        after_all_pending = bals[slot] - pending_tot

        await update.message.reply_text(
            "Riepilogo provvisorio:\n"
            f"• Slot {slot}\n"
            f"• kWh {fmt_kwh(kwh)}\n"
            f"Saldo attuale Slot {slot}: {fmt_kwh(bals[slot])}\n"
            f"→ Dopo QUESTA: {fmt_kwh(after_only_this)} kWh\n"
            f"→ Dopo TUTTE le PENDING (incl. questa): {fmt_kwh(after_all_pending)} kWh\n"
            "Ora invia la foto della ricarica."
        )
        return

    # Inserimento nota
    elif step == "await_note":
        note = (update.message.text or "").strip()[:500]
        context.user_data["pending_note"] = note
        context.user_data["pending_step"] = "confirm"
        await finalize_and_send(update, context, qmessage=False)
        return

# Ricezione foto
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard_approved(update, context):
        return
    if context.user_data.get("pending_step") != "await_photo":
        return
    photo = update.message.photo[-1]
    context.user_data["pending_photo_id"] = photo.file_id
    context.user_data["pending_note"] = ""
    context.user_data["pending_step"] = "confirm"
    await update.message.reply_text(
        "Foto ricevuta. Vuoi aggiungere una nota o confermare la ricarica?",
        reply_markup=post_photo_keyboard(),
    )

# Post-foto: pulsanti
async def on_post_photo_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await guard_approved(update, context):
        return
    data = q.data

    if data == "flow:cancel":
        context.user_data.clear()
        await q.edit_message_text("Operazione annullata.")
        return

    if data == "flow:add_note":
        if context.user_data.get("pending_step") not in ("confirm",):
            await q.answer("Sequenza non valida.", show_alert=True)
            return
        context.user_data["pending_step"] = "await_note"
        await q.edit_message_text("Scrivi ora la nota (max 500 caratteri).")
        return

    if data == "flow:confirm":
        if context.user_data.get("pending_step") not in ("confirm",):
            await q.answer("Sequenza non valida.", show_alert=True)
            return
        await finalize_and_send(update, context, qmessage=True)
        return

# Finalizzazione richiesta e invio agli admin
async def finalize_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, qmessage=False):
    u = update.effective_user
    ensure_user(u)

    try:
        slot = int(context.user_data.get("pending_slot"))
        kwh  = qk(Decimal(context.user_data.get("pending_kwh")))
        file_id = context.user_data.get("pending_photo_id")
        note = context.user_data.get("pending_note", "")
        assert slot in SLOTS and kwh > 0 and file_id
    except Exception:
        if qmessage and update.callback_query:
            await update.callback_query.edit_message_text("Dati incompleti. Annulla e riparti.")
        else:
            await update.message.reply_text("Dati incompleti. Annulla e riparti.")
        context.user_data.clear()
        return

# Blocco opzionale ricariche che porterebbero il saldo sotto zero (considerando tutte le pending)
if not ALLOW_NEGATIVE:
    current_bal = get_balances(u.id)[slot]
    pending_tot = sum_pending_for(u.id, slot) + kwh
    if (current_bal - pending_tot) < Decimal("0"):
        warn = ("La richiesta supererebbe il saldo disponibile considerando le pending. "
                "Riduci i kWh o attendi l'approvazione di richieste precedenti.")
        if qmessage and update.callback_query:
            await update.callback_query.edit_message_text(warn)
        else:
            await update.message.reply_text(warn)
        return
# Rate limit: one finalize per 60s per user
remaining = check_rate_limit(u.id, "finalize", 60)
if remaining > 0:
    msg = f"Hai appena inviato una richiesta. Riprova tra {remaining} secondi."
    if qmessage and update.callback_query:
        await update.callback_query.edit_message_text(msg)
    else:
        await update.message.reply_text(msg)
    return

    rid = create_recharge(u.id, slot, kwh, file_id, note=note)
    bals = get_balances(u.id)
    pending_tot = sum_pending_for(u.id, slot)   # include questa
    after_only_this = bals[slot] - kwh
    after_all_pending = bals[slot] - pending_tot

    caption = (
        f"Richiesta ricarica\n"
        f"ID: #{rid}\n"
        f"Utente: {u.first_name or ''} @{u.username or ''} (id {u.id})\n"
        f"Slot: {slot}\n"
        f"kWh: {fmt_kwh(kwh)}\n"
        f"Saldo attuale Slot {slot}: {fmt_kwh(bals[slot])}\n"
        f"→ Dopo QUESTA: {fmt_kwh(after_only_this)} kWh\n"
        f"→ Dopo TUTTE le PENDING: {fmt_kwh(after_all_pending)} kWh\n"
    )
    if note:
        caption += f"Nota: {note}\n"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Approva", callback_data=f"approve:{rid}"),
         InlineKeyboardButton("Rifiuta",  callback_data=f"reject:{rid}")]
    ])

    sent_to_any = False
    for aid in ADMIN_IDS:
        try:
            msg = await context.bot.send_photo(chat_id=aid, photo=file_id, caption=caption, reply_markup=kb)
            save_admin_notification(rid, aid, msg.message_id)
            sent_to_any = True
        except:
            pass

    user_text = (
        f"Richiesta inviata all'amministrazione (Slot {slot}, {fmt_kwh(kwh)} kWh)."
        if sent_to_any else
        "Richiesta registrata, ma non è stato possibile avvisare gli amministratori."
    )

    context.user_data.clear()
    if qmessage and update.callback_query:
        try:
            await update.callback_query.edit_message_text("Richiesta inviata.")
        except:
            pass
        await update.callback_query.message.chat.send_message(user_text, reply_markup=main_keyboard(update.effective_user.id))
    else:
        await update.message.reply_text(user_text, reply_markup=main_keyboard(update.effective_user.id))

# Review ricarica (admin)
async def on_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer(f"Solo gli amministratori possono approvare.\nTu: {q.from_user.id}", show_alert=True)
        return

    try:
        action, rid_s = q.data.split(":")
        rid = int(rid_s)
    except Exception:
        try:
            await q.edit_message_caption("Errore: callback non valida.")
        except:
            await context.bot.send_message(chat_id=q.message.chat.id, text="Errore: callback non valida.")
        return

    rec = get_recharge(rid)
    if not rec:
        try:
            await q.edit_message_caption("Questa richiesta non esiste più.")
        except:
            await context.bot.send_message(chat_id=q.message.chat.id, text="Questa richiesta non esiste più.")
        return

    _id, u_id, slot, kwh_f, file_id, status, note = rec
    if status != "pending":
        txt = f"Richiesta #{rid} già {status}."
        try:
            await q.edit_message_caption(txt, reply_markup=None)
        except:
            await context.bot.send_message(chat_id=q.message.chat.id, text=txt)
        return

    kwh = qk(Decimal(str(kwh_f)))

    if action == "approve":
        add_balance_slot(u_id, slot, -kwh)
        set_recharge_status(rid, "approved", q.from_user.id)
        bals = get_balances(u_id)

        pending_left = sum_pending_for(u_id, slot)
        after_all_pending = bals[slot] - pending_left

        try:
            await context.bot.send_message(
                chat_id=u_id,
                text=(f"Ricarica #{rid} approvata.\n"
                      f"Slot {slot}: −{fmt_kwh(kwh)} kWh.\n"
                      f"Nuovo saldo Slot {slot}: {fmt_kwh(bals[slot])} kWh.\n"
                      f"PENDING rimanenti su Slot {slot}: {fmt_kwh(pending_left)} kWh "
                      f"(saldo stimato dopo tutte: {fmt_kwh(after_all_pending)} kWh).")
            )
        except:
            pass

        admin_txt = (f"Richiesta #{rid} APPROVATA.\n"
                     f"Utente {u_id} – Slot {slot} −{fmt_kwh(kwh)} kWh → saldo {fmt_kwh(bals[slot])} kWh.\n"
                     f"PENDING residue su Slot {slot}: {fmt_kwh(pending_left)} kWh "
                     f"(dopo tutte: {fmt_kwh(after_all_pending)} kWh).")

        try:
            await q.edit_message_caption(admin_txt, reply_markup=None)
        except:
            await context.bot.send_message(chat_id=q.message.chat.id, text=admin_txt)

        for aid, mid in get_admin_notifications(rid):
            if aid == q.from_user.id and mid == getattr(q.message, "message_id", None):
                continue
            try:
                await context.bot.edit_message_caption(chat_id=aid, message_id=mid, caption=admin_txt, reply_markup=None)
            except:
                try:
                    await context.bot.send_message(chat_id=aid, text=admin_txt)
                except:
                    pass

    elif action == "reject":
        set_recharge_status(rid, "rejected", q.from_user.id)
        try:
            await context.bot.send_message(chat_id=u_id, text=f"Ricarica #{rid} (Slot {slot}) rifiutata dall'amministrazione.")
        except:
            pass

        admin_txt = f"Richiesta #{rid} RIFIUTATA."
        try:
            await q.edit_message_caption(admin_txt, reply_markup=None)
        except:
            await context.bot.send_message(chat_id=q.message.chat.id, text=admin_txt)

        for aid, mid in get_admin_notifications(rid):
            if aid == q.from_user.id and mid == getattr(q.message, "message_id", None):
                continue
            try:
                await context.bot.edit_message_caption(chat_id=aid, message_id=mid, caption=admin_txt, reply_markup=None)
            except:
                try:
                    await context.bot.send_message(chat_id=aid, text=admin_txt)
                except:
                    pass

# Admin: accredito manuale
async def credita(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 3:
        await update.message.reply_text("Uso: /credita <user_id> <slot> <kwh>\nEsempio: /credita 123456 8 10.5")
        return
    try:
        uid = int(context.args[0])
        slot = int(context.args[1])
        if slot not in SLOTS:
            raise ValueError("Slot non valido (usa 8, 3 o 5).")
        kwh = parse_kwh_any(context.args[2])  # può essere negativo
        add_balance_slot(uid, slot, kwh)
        bals = get_balances(uid)
        await update.message.reply_text(
            f"Variazione {('+' if kwh>=0 else '')}{fmt_kwh(kwh)} kWh all'utente {uid} su Slot {slot}. "
            f"Nuovo saldo Slot {slot}: {fmt_kwh(bals[slot])} kWh."
        )
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(f"Wallet Slot {slot} aggiornato: {('+' if kwh>=0 else '')}{fmt_kwh(kwh)} kWh.\n"
                      f"Nuovo saldo Slot {slot}: {fmt_kwh(bals[slot])} kWh.")
            )
        except:
            pass
    except Exception as e:
        await update.message.reply_text(f"Errore: {e}")

# Admin: elenco utenti
async def utenti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    # /utenti [tutti|approvati|pending] [pagina] [cerca <termine...>]
    status = "approved"
    page = 1
    search = None

    args = [a.strip() for a in context.args] if context.args else []
    i = 0
    # primo arg: stato
    if i < len(args):
        v = args[i].lower()
        if v in ("tutti", "all"):
            status = "tutti"
            i += 1
        elif v in ("approvati", "approved"):
            status = "approved"
            i += 1
        elif v in ("pending", "inattesa", "nonapprovati"):
            status = "pending"
            i += 1
    # secondo arg: pagina
    if i < len(args):
        try:
            page = max(1, int(args[i]))
            i += 1
        except:
            pass
    # cerca ...
    if i < len(args):
        if args[i].lower() in ("cerca", "search"):
            i += 1
            if i < len(args):
                search = " ".join(args[i:]).strip()
        else:
            # se scrive direttamente un termine, lo interpreto come search
            search = " ".join(args[i:]).strip()

    per_page = 20
    offset = (page - 1) * per_page
    filter_status = "approved" if status == "approved" else ("pending" if status == "pending" else "tutti")

    total = count_users("approved" if filter_status == "approved"
                        else ("pending" if filter_status == "pending" else "tutti"),
                        search=search)

    rows = list_users(
        filter_status=("approved" if filter_status == "approved"
                       else "pending" if filter_status == "pending" else "tutti"),
        limit=per_page, offset=offset, search=search
    )

    if not rows:
        await update.message.reply_text("Nessun utente trovato per i criteri richiesti.")
        return

    lines = []
    for r in rows:
        uid, uname, fn, ln, b8, b3, b5, appr, created_at = r
        pend = pending_count_for_user(uid)
        created_fmt = "-"
        try:
            # stampo in UTC ISO corto
            created_fmt = (created_at or "-").replace("T", " ")[:19]
        except:
            pass
        lines.append(
            f"ID {uid} @{uname or '-'} | {fn or ''} {ln or ''} | "
            f"Approvato: {'✔' if int(appr)==1 else '✖'} | "
            f"S8:{fmt_kwh(Decimal(str(b8)))} S3:{fmt_kwh(Decimal(str(b3)))} S5:{fmt_kwh(Decimal(str(b5)))} | "
            f"Pending:{pend} | Iscrizione: {created_fmt}"
        )

    pages = (total + per_page - 1) // per_page if total > 0 else 1
    header = f"Utenti ({status}{f', cerca: {search}' if search else ''}) – pagina {page}/{pages} – tot: {total}"
    await update.message.reply_text(header + "\n" + "\n".join(lines))

# Utility
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Il tuo chat_id è: {update.effective_user.id}")




async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with db() as conn:
        cur = conn.execute("SELECT id FROM recharges WHERE status='pending' ORDER BY id DESC")
        ids = [r[0] for r in cur.fetchall()]
    if not ids:
        await update.message.reply_text("Nessuna ricarica in attesa.")
        return
    context.user_data["pending_ids"] = ids
    context.user_data["pending_idx"] = 0
    await render_pending_card(update, context, 0)

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Uso: /export users | /export recharges [YYYY-MM-DD] [YYYY-MM-DD]")
        return

    what = args[0].lower()

    import csv, io
    if what == "users":
        with db() as conn:
            cur = conn.execute("""
                SELECT user_id, username, first_name, last_name,
                       COALESCE(balance_slot8,0), COALESCE(balance_slot3,0), COALESCE(balance_slot5,0),
                       COALESCE(approved,0), COALESCE(created_at,'')
                FROM users
                ORDER BY user_id ASC
            """)
            rows = cur.fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["user_id","username","first_name","last_name","balance_slot8","balance_slot3","balance_slot5","approved","created_at"])
        for r in rows:
            w.writerow(r)
        data = io.BytesIO(buf.getvalue().encode("utf-8"))
        data.name = "users.csv"
        await context.bot.send_document(chat_id=update.effective_chat.id, document=data, caption="Esportazione utenti")
        return

    if what == "recharges":
        date_from = args[1] if len(args) >= 2 else None
        date_to   = args[2] if len(args) >= 3 else None
        where = ["1=1"]
        params = []
        if date_from:
            where.append("created_at >= ?")
            params.append(date_from)
        if date_to:
            where.append("created_at <= ?||'T23:59:59'")
        query = f"""
            SELECT id, user_id, slot, kwh, status, COALESCE(created_at,''), COALESCE(reviewed_at,''), COALESCE(reviewer_id,''), COALESCE(note,''), COALESCE(photo_file_id,'')
            FROM recharges
            WHERE {' AND '.join(where)}
            ORDER BY id ASC
        """
        with db() as conn:
            cur = conn.execute(query, tuple(params))
            rows = cur.fetchall()
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id","user_id","slot","kwh","status","created_at","reviewed_at","reviewer_id","note","photo_file_id"])
        for r in rows:
            w.writerow(r)
        data = io.BytesIO(buf.getvalue().encode("utf-8"))
        data.name = "recharges.csv"
        await context.bot.send_document(chat_id=update.effective_chat.id, document=data, caption="Esportazione ricariche")
        return

    await update.message.reply_text("Uso: /export users | /export recharges [YYYY-MM-DD] [YYYY-MM-DD]")


async def on_pending_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Solo gli admin.", show_alert=True)
        return
    try:
        _, kind, rid_s = q.data.split(":", 2)
        rid = int(rid_s)
    except Exception:
        await q.answer("Callback non valida", show_alert=True)
        return

    with db() as conn:
        cur = conn.execute("""
            SELECT r.id, r.user_id, r.slot, r.kwh, r.status, COALESCE(r.created_at,''), COALESCE(r.reviewed_at,''),
                   COALESCE(r.reviewer_id,''), COALESCE(r.note,''), COALESCE(r.photo_file_id,''),
                   COALESCE(u.username,''), COALESCE(u.first_name,''), COALESCE(u.last_name,'')
            FROM recharges r
            LEFT JOIN users u ON u.user_id = r.user_id
            WHERE r.id = ?
        """, (rid,))
        row = cur.fetchone()

    if not row:
        await q.edit_message_text("Ricarica non trovata (forse già gestita).")
        return

    (_rid, uid, slot, kwh, status, created_at, reviewed_at, reviewer_id, note, photo_id, username, first_name, last_name) = row
    if kind == "photo":
        if photo_id:
            # send as a new message to keep the list intact
            await context.bot.send_photo(chat_id=q.message.chat.id, photo=photo_id, caption=f"#{rid} – foto scontrino (slot {slot}, {fmt_kwh(Decimal(str(kwh)))} kWh)")
        else:
            await q.answer("Nessuna foto allegata.", show_alert=True)
        return

    if kind == "info":
        name = (first_name or "") + (" " + last_name if last_name else "")
        user_line = f"@{username}" if username else name.strip() or str(uid)
        created_fmt = (created_at or "-").replace("T"," ")[:19]
        reviewed_fmt = (reviewed_at or "-").replace("T"," ")[:19]
        info = [
            f"Ricarica #{rid}",
            f"Utente: {user_line} (id {uid})",
            f"Slot: {slot}",
            f"kWh richiesti: {fmt_kwh(Decimal(str(kwh)))}",
            f"Stato: {status}",
            f"Creata: {created_fmt}",
            f"Revisionata: {reviewed_fmt}",
        ]
        if reviewer_id:
            info.append(f"Revisore: {reviewer_id}")
        if note:
            info.append(f"Nota: {note}")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📷 Foto", callback_data=f"pending:photo:{rid}")],
            [InlineKeyboardButton("✅ Approva", callback_data=f"approve:{rid}"), InlineKeyboardButton("❌ Rifiuta", callback_data=f"reject:{rid}")]
        ])
        try:
            await q.edit_message_text("\n".join(info), reply_markup=kb)
        except:
            await context.bot.send_message(chat_id=q.message.chat.id, text="\n".join(info), reply_markup=kb)


# ---- Rate limit (in-memory) ----
import time
from datetime import datetime, timezone, timedelta
RATE_LIMIT = {}  # {(user_id, key): last_ts}
def check_rate_limit(user_id: int, key: str, window: int) -> int:
    """
    Return remaining seconds if still limited, else 0.
    """
    now = int(time.time())
    last = RATE_LIMIT.get((user_id, key), 0)
    if now - last < window:
        return window - (now - last)
    RATE_LIMIT[(user_id, key)] = now
    return 0

# ---- Helpers to render pending card with pagination ----
PENDING_PAGE_SIZE = 1
async def render_pending_card(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    ids = context.user_data.get("pending_ids") or []
    if not ids:
        try:
            await update.callback_query.edit_message_text("Nessuna ricarica in attesa.")
        except:
            await update.effective_message.reply_text("Nessuna ricarica in attesa.")
        return

    if idx < 0: idx = 0
    if idx >= len(ids): idx = len(ids) - 1
    context.user_data["pending_idx"] = idx
    rid = ids[idx]

    with db() as conn:
        cur = conn.execute("""
            SELECT r.id, r.user_id, r.slot, r.kwh, r.status, COALESCE(r.created_at,''),
                   COALESCE(r.note,''), COALESCE(r.photo_file_id,''),
                   COALESCE(u.username,''), COALESCE(u.first_name,''), COALESCE(u.last_name,'')
            FROM recharges r
            LEFT JOIN users u ON u.user_id = r.user_id
            WHERE r.id = ?
        """, (rid,))
        row = cur.fetchone()

    if not row:
        text = f"Ricarica #{rid} non trovata."
        try:
            await update.callback_query.edit_message_text(text)
        except:
            await update.effective_message.reply_text(text)
        return

    (_rid, uid, slot, kwh, status, created_at, note, photo_id, username, first_name, last_name) = row
    name = (first_name or "") + (" " + last_name if last_name else "")
    user_line = f"@{username}" if username else name.strip() or str(uid)
    created_fmt = (created_at or "-").replace("T"," ")[:19]
    text = (
        f"[{idx+1}/{len(ids)}] Ricarica #{_rid} • {created_fmt}\n"
        f"Utente: {user_line} (id {uid})\n"
        f"Slot: {slot} • Richiesta: {fmt_kwh(Decimal(str(kwh)))} kWh\n"
        + (f"Nota: {note}" if note else "")
    ).strip()

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️", callback_data="pending:nav:prev"),
         InlineKeyboardButton("➡️", callback_data="pending:nav:next")],
        [InlineKeyboardButton("📷 Foto", callback_data=f"pending:photo:{_rid}"),
         InlineKeyboardButton("ℹ️ Dettagli", callback_data=f"pending:info:{_rid}")],
        [InlineKeyboardButton("✅ Approva", callback_data=f"approve:{_rid}"),
         InlineKeyboardButton("❌ Rifiuta", callback_data=f"reject:{_rid}")],
    ])

    # Render/Update
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        except:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)


def migrate_extra():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallet_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount_eur NUMERIC NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                reviewed_at TEXT,
                reviewer_id INTEGER,
                note TEXT
            )
        """)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN wallet_kwh NUMERIC DEFAULT 0")
        except Exception:
            pass

try:
    migrate_extra()
except NameError:
    pass
except Exception as _e:
    print("[WARN] migrate_extra:", _e)


async def on_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "wallet:req":
        context.user_data["awaiting_wallet_amount_eur"] = True
        await q.edit_message_text("Inserisci l'importo in € (es. 15.50).")

async def on_message_amount_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_wallet_amount_eur"):
        return
    txt = (update.message.text or "").replace(",", ".").strip()
    try:
        amt = Decimal(txt)
        if amt <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Importo non valido. Inserisci un numero positivo, es. 10 o 12.50")
        return
    with db() as conn:
        conn.execute("INSERT INTO wallet_requests (user_id, amount_eur, status) VALUES (?,?, 'pending')",
                     (update.effective_user.id, str(amt)))
    context.user_data["awaiting_wallet_amount_eur"] = False
    await update.message.reply_text(f"Richiesta inviata: € {amt}. Un admin valuterà la ricarica.")


async def wallet_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with db() as conn:
        cur = conn.execute("SELECT id FROM wallet_requests WHERE status='pending' ORDER BY id DESC")
        ids = [r[0] for r in cur.fetchall()]
    if not ids:
        await update.message.reply_text("Nessuna richiesta wallet in attesa.")
        return
    context.user_data["wallet_ids"] = ids
    context.user_data["wallet_idx"] = 0
    await render_wallet_card(update, context, 0)

async def render_wallet_card(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    ids = context.user_data.get("wallet_ids") or []
    if not ids:
        await update.effective_message.reply_text("Nessuna richiesta.")
        return
    if idx < 0: idx = 0
    if idx >= len(ids): idx = len(ids)-1
    context.user_data["wallet_idx"] = idx
    wid = ids[idx]
    with db() as conn:
        cur = conn.execute("""
            SELECT w.id, w.user_id, w.amount_eur, w.status, COALESCE(w.created_at,''), COALESCE(w.note,''),
                   COALESCE(u.username,''), COALESCE(u.first_name,''), COALESCE(u.last_name,''), COALESCE(u.wallet_kwh,0)
            FROM wallet_requests w
            LEFT JOIN users u ON u.user_id = w.user_id
            WHERE w.id = ?
        """, (wid,))
        row = cur.fetchone()
    if not row:
        await update.effective_message.reply_text(f"Richiesta #{wid} non trovata."); return
    (_id, uid, amt, status, created_at, note, username, first_name, last_name, wallet_kwh) = row
    created_fmt = (created_at or "-").replace("T"," ")[:19]
    who = f"@{username}" if username else ((first_name or "") + (" " + last_name if last_name else "")).strip() or str(uid)
    text = (f"[{idx+1}/{len(ids)}] Wallet #{_id} • {created_fmt}\n"
            f"Utente: {who} (id {uid})\n"
            f"Importo richiesto: € {amt}\n"
            f"Wallet attuale: {fmt_kwh(Decimal(str(wallet_kwh)))} kWh")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️", callback_data="wallet:nav:prev"),
         InlineKeyboardButton("➡️", callback_data="wallet:nav:next")],
        [InlineKeyboardButton("✅ Accetta", callback_data=f"wallet:accept:{_id}"),
         InlineKeyboardButton("❌ Rifiuta", callback_data=f"wallet:reject:{_id}")]
    ])
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        except:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, reply_markup=kb)

async def on_wallet_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Solo admin", show_alert=True); return
    data = q.data
    if data.startswith("wallet:nav:"):
        direction = data.split(":")[-1]
        idx = int(context.user_data.get("wallet_idx", 0))
        idx = idx + 1 if direction == "next" else idx - 1
        await render_wallet_card(update, context, idx)
        return
    if data.startswith("wallet:accept:"):
        wid = int(data.split(":")[-1])
        context.user_data["awaiting_wallet_kwh_for"] = wid
        await q.edit_message_text(f"Inserisci i kWh da accreditare per richiesta #{wid}.")
        return
    if data.startswith("wallet:reject:"):
        wid = int(data.split(":")[-1])
        with db() as conn:
            conn.execute("UPDATE wallet_requests SET status='rejected', reviewed_at=datetime('now'), reviewer_id=? WHERE id=?",
                         (q.from_user.id, wid))
        await q.edit_message_text(f"Richiesta #{wid} rifiutata.")
try:
    with db() as conn:
        cur = conn.execute("SELECT user_id FROM wallet_requests WHERE id=?", (wid,))
        r = cur.fetchone()
    if r:
        await context.bot.send_message(chat_id=r[0], text="La tua richiesta wallet è stata rifiutata.")
except Exception:
    pass
        return

async def on_message_admin_wallet_kwh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wid = context.user_data.get("awaiting_wallet_kwh_for")
    if not wid:
        return
    txt = (update.message.text or "").replace(",", ".").strip()
    try:
        kwh = Decimal(txt)
        if kwh <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Valore non valido. Inserisci un numero positivo (kWh).")
        return
    with db() as conn:
        cur = conn.execute("SELECT user_id FROM wallet_requests WHERE id=? AND status='pending'", (wid,))
        r = cur.fetchone()
        if not r:
            await update.message.reply_text("Richiesta non trovata o già processata.")
            context.user_data.pop("awaiting_wallet_kwh_for", None)
            return
        uid = r[0]
        conn.execute("UPDATE users SET wallet_kwh = COALESCE(wallet_kwh,0) + ? WHERE user_id=?", (str(kwh), uid))
        conn.execute("UPDATE wallet_requests SET status='approved', reviewed_at=datetime('now'), reviewer_id=? WHERE id=?",
                     (update.effective_user.id, wid))
    context.user_data.pop("awaiting_wallet_kwh_for", None)
    await update.message.reply_text(f"Accreditati {fmt_kwh(kwh)} kWh sul wallet dell'utente {uid}.")
try:
    await context.bot.send_message(chat_id=uid, text=f"La tua richiesta wallet è stata approvata. Accreditati {fmt_kwh(kwh)} kWh.")
except Exception:
    pass



async def delete_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Solo admin", show_alert=True); return
    context.user_data["awaiting_delete_user"] = True
    await q.edit_message_text("Invia l'ID utente da eliminare. (Operazione irreversibile)")

async def on_message_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_delete_user"):
        return
    txt = (update.message.text or "").strip()
    try:
        uid = int(txt)
    except Exception:
        await update.message.reply_text("ID/utente non valido. Puoi inserire anche @username o nome.")
        return
    with db() as conn:
        conn.execute("DELETE FROM users WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM recharges WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM wallet_requests WHERE user_id=?", (uid,))
    context.user_data["awaiting_delete_user"] = False
    await update.message.reply_text(f"Utente {uid} eliminato.")


    # ---- User resolution & wallet helpers ----
    def get_wallet_kwh(user_id: int) -> Decimal:
        with db() as conn:
            cur = conn.execute("SELECT COALESCE(wallet_kwh,0) FROM users WHERE user_id=?", (user_id,))
            row = cur.fetchone()
            return Decimal(str(row[0])) if row else Decimal("0")

    def resolve_user_identifier(q: str):
        """
        Accepts numeric id, @username, or name part. Returns:
          - int user_id if exact/unique
          - list of (user_id, username, first_name, last_name) if multiple
          - None if not found
        """
        q = (q or "").strip()
        if not q:
            return None
        try:
            return int(q)
        except Exception:
            pass
        uname = q[1:] if q.startswith("@") else q
        with db() as conn:
            cur = conn.execute("SELECT user_id, username, first_name, last_name FROM users WHERE LOWER(username)=LOWER(?)", (uname,))
            row = cur.fetchone()
            if row:
                return row[0]
            like = f"%{q}%"
            cur = conn.execute(
                "SELECT user_id, COALESCE(username,''), COALESCE(first_name,''), COALESCE(last_name,'') "
                "FROM users "
                "WHERE (IFNULL(username,'') LIKE ? OR IFNULL(first_name,'') LIKE ? OR IFNULL(last_name,'') LIKE ?) "
                "ORDER BY user_id ASC LIMIT 10",
                (like, like, like)
            )
            rows = cur.fetchall()
        if not rows:
            return None
        if len(rows) == 1:
            return rows[0][0]
        return rows
    

async def ask_user_pick(update: Update, rows, tag: str):
    buttons = []
    for uid, username, fn, ln in rows:
        name = (fn or "") + (" " + ln if ln else "")
        who = f"@{username}" if username else (name.strip() or str(uid))
        label = f"{who} (id {uid})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"userpick:{tag}:{uid}")])
    await update.message.reply_text("Seleziona utente:", reply_markup=InlineKeyboardMarkup(buttons))


async def on_userpick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, tag, uid_s = q.data.split(":", 2)
        uid = int(uid_s)
    except Exception:
        await q.answer("Selezione non valida", show_alert=True); return

    # SALDO
    if tag == "saldo":
        if not is_admin(q.from_user.id):
            await q.answer("Solo admin", show_alert=True); return
        b = get_balances(uid)
        wallet = get_wallet_kwh(uid)
        await q.edit_message_text(
            "Saldi utente {oid}:\n"
            f"• Slot 8: {fmt_kwh(b[8])} kWh\n"
            f"• Slot 3: {fmt_kwh(b[3])} kWh\n"
            f"• Slot 5: {fmt_kwh(b[5])} kWh\n"
            f"• Wallet: {fmt_kwh(wallet)} kWh".format(oid=uid),
            reply_markup=main_keyboard(q.from_user.id)
        )
        return

    # DELETE
    if tag == "delete":
        if not is_admin(q.from_user.id):
            await q.answer("Solo admin", show_alert=True); return
        with db() as conn:
            conn.execute("DELETE FROM users WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM recharges WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM wallet_requests WHERE user_id=?", (uid,))
        await q.edit_message_text(f"Utente {uid} eliminato.")
        return


async def on_userdel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Solo admin", show_alert=True); return
    try:
        _, choice, uid_s = q.data.split(":", 2)
        uid = int(uid_s)
    except Exception:
        await q.answer("Selezione non valida", show_alert=True); return
    if choice == "no":
        await q.edit_message_text("Operazione annullata.")
        return
    # choice == yes
    with db() as conn:
        conn.execute("DELETE FROM users WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM recharges WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM wallet_requests WHERE user_id=?", (uid,))
    await q.edit_message_text(f"Utente {uid} eliminato.")


async def startup_notify(app: "Application"):
    try:
        # Boot log prints
        print("[BOOT] saldo-bot avviato ✅")
        print(f"Python: {sys.version.split()[0]} • PTB: {telegram.__version__}")
        print(f"DB_PATH: {os.getenv('DB_PATH', 'kwh_slots.db')}")
        # Count registered handlers (best effort)
        try:
            total_handlers = sum(len(h) for h in app.handlers.values())
        except Exception:
            total_handlers = 0
        print(f"Handlers: {total_handlers}")
        # Telegram notify to admins
        admins = []
        if os.getenv("ADMIN_IDS"):
            admins = [int(x.strip()) for x in os.getenv("ADMIN_IDS").split(",") if x.strip().isdigit()]
        elif os.getenv("ADMIN_ID"):
            admins = [int(os.getenv("ADMIN_ID"))]
        if admins:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            for aid in admins:
                try:
                    await app.bot.send_message(chat_id=aid, text=f"🔔 saldo-bot avviato\n{ts}\nPython {sys.version.split()[0]} • PTB {telegram.__version__}")
                except Exception as e:
                    print("[BOOT] notify admin failed:", aid, e)
    except Exception as e:
        print("[BOOT] startup_notify error:", e)


async def daily_ping(context: ContextTypes.DEFAULT_TYPE):
    try:
        admins = []
        if os.getenv("ADMIN_IDS"):
            admins = [int(x.strip()) for x in os.getenv("ADMIN_IDS").split(",") if x.strip().isdigit()]
        elif os.getenv("ADMIN_ID"):
            admins = [int(os.getenv("ADMIN_ID"))]
        if not admins:
            return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        for aid in admins:
            try:
                await context.bot.send_message(chat_id=aid, text=f"🏓 Ping giornaliero: bot attivo\n{ts}")
            except Exception as e:
                print("[PING] notify admin failed:", aid, e)
    except Exception as e:
        print("[PING] error:", e)

# ==== MAIN ====
def main():
    init_db()
    if not TOKEN or not ADMIN_IDS:
        raise RuntimeError("Imposta TELEGRAM_TOKEN e ADMIN_IDS (o ADMIN_ID) nel file .env o nelle variabili d'ambiente.")

    # —— PRE-FLIGHT: test connessione e token —— #
    try:
        import httpx
        url = f"https://api.telegram.org/bot{TOKEN}/getMe"
        r = httpx.get(url, timeout=20.0)
        if r.status_code == 401:
            print("❌ ERRORE: Token non valido (401). Rigenera da @BotFather e aggiorna .env")
            return
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            print("❌ ERRORE: risposta Telegram non OK:", data)
            return
        bot = data.get("result", {})
        print(f"✅ Connessione OK – Bot: {bot.get('first_name')} (@{bot.get('username')})")
    except httpx.ConnectTimeout:
        print("❌ ERRORE: Timeout di connessione a Telegram.\n– Prova altra rete/hotspot\n– Controlla firewall/antivirus (api.telegram.org:443)")
        return
    except Exception as e:
        print("❌ ERRORE di rete/token:", e)
        return

    app = Application.builder().token(TOKEN).build()

    # Comandi
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saldo", saldo))
    app.add_handler(CommandHandler("annulla", annulla))
    app.add_handler(CommandHandler("credita", credita))
    app.add_handler(CommandHandler("whoami", whoami))
    # Post-init: send boot notification and print logs
    try:
        app.post_init.append(startup_notify)
    except Exception as _e:
        print("[BOOT] cannot attach startup_notify:", _e)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_amount_wallet))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_admin_wallet_kwh))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_delete_user))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("utenti", utenti))
    app.add_handler(CommandHandler("walletpending", wallet_pending))  # nuovo comando admin

    # Flusso ricarica
    app.add_handler(MessageHandler(filters.Regex(r"^➕ Ricarica$"), on_ricarica_button))
    app.add_handler(CallbackQueryHandler(on_slot_choice, pattern=r"^slot:\d+$|^flow:cancel$"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_post_photo_buttons, pattern=r"^flow:(add_note|confirm|cancel)$"))

    # Handler unificato per testo (kWh + nota)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_input))

    # Approvazione utenti
    app.add_handler(CallbackQueryHandler(on_user_approval, pattern=r"^user:(approve|reject):\d+$"))

    # Review ricariche (admin)
    app.add_handler(CallbackQueryHandler(on_review, pattern=r"^(approve|reject):\d+$"))
    app.add_handler(CallbackQueryHandler(on_pending_action, pattern=r"^pending:(photo|info):\d+$"))

    app.run_polling()

if __name__ == "__main__":
    main()
