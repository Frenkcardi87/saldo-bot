#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
saldo-bot
- PTB v20 async
- SQLite persistence
- Funzioni principali:
  • /start /help /whoami
  • Menu: 📊 Saldo, 📝 Dichiara ricarica (foto obbligatoria), 💳 Wallet, ℹ️ Help
    (se admin: 🧾 Pending, 👛 Wallet pending, 👥 Utenti)
  • /saldo (utente) + /saldo <utente> (admin)
  • /pending con paginazione; foto/info; Approva/Rifiuta
  • /utenti con paginazione; elimina utente; ricerca
  • /export users / recharges
  • Richiesta wallet: utente → € → notifica admin con pulsanti Accetta/Rifiuta → kWh + notifica utente
  • Approva/Rifiuta ricarica: notifica utente e aggiornamento saldo slot
  • Startup logs + notifica avvio + ping giornaliero + error handler globale
"""

import os
import sys
import sqlite3
import logging
import time
from typing import Optional, Dict, List, Tuple
from decimal import Decimal, ROUND_HALF_UP
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import telegram
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---- CONFIG --------------------

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "kwh_slots.db")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "")).replace(",", " ").split() if x.strip().isdigit())
ALLOW_NEGATIVE = os.getenv("ALLOW_NEGATIVE", "1").strip().lower() in {"1","true","yes","y"}

# -------------------- LOGGING --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("saldo-bot")

# -------------------- DB --------------------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def migrate():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                approved INTEGER DEFAULT 0,
                balance_slot8 NUMERIC DEFAULT 0,
                balance_slot3 NUMERIC DEFAULT 0,
                balance_slot5 NUMERIC DEFAULT 0,
                wallet_kwh NUMERIC DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS recharges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                slot INTEGER NOT NULL,
                kwh NUMERIC NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
                note TEXT,
                photo_file_id TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                reviewed_at TEXT,
                reviewer_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS wallet_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount_eur NUMERIC NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                reviewed_at TEXT,
                reviewer_id INTEGER
            );

            CREATE INDEX IF NOT EXISTS ix_recharges_status ON recharges(status);
            CREATE INDEX IF NOT EXISTS ix_wallet_status ON wallet_requests(status);
            """
        )

# -------------------- UTILS --------------------

def is_admin(user_id: int) -> bool:
    return int(user_id) in ADMIN_IDS

def ensure_user(u: telegram.User):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, approved)
            VALUES (?, ?, ?, ?, COALESCE(?, 0))
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name
            """,
            (u.id, u.username, u.first_name, u.last_name, 1 if is_admin(u.id) else 0),
        )

def fmt_kwh(x: Decimal | float | int | str) -> str:
    d = Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = format(d.normalize(), "f")
    return s

def get_balances(user_id: int) -> Dict[int, Decimal]:
    with db() as conn:
        cur = conn.execute(
            "SELECT COALESCE(balance_slot8,0), COALESCE(balance_slot3,0), COALESCE(balance_slot5,0) FROM users WHERE user_id=?",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return {8: Decimal("0"),3: Decimal("0"),5: Decimal("0")}
    return {8: Decimal(str(row[0])), 3: Decimal(str(row[1])), 5: Decimal(str(row[2]))}

def get_wallet_kwh(user_id: int) -> Decimal:
    with db() as conn:
        cur = conn.execute("SELECT COALESCE(wallet_kwh,0) FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
    return Decimal(str(row[0])) if row else Decimal("0")

def resolve_user_identifier(q: str):
    """
    Accept numeric id, @username, or partial name.
    Returns int user_id, or list of matches (user_id, username, first_name, last_name), or None.
    """
    q = (q or "").strip()
    if not q:
        return None
    if q.startswith("@"):
        with db() as conn:
            cur = conn.execute("SELECT user_id, username, first_name, last_name FROM users WHERE username=?", (q[1:],))
            r = cur.fetchone()
        return int(r[0]) if r else None
    if q.isdigit():
        return int(q)
    # partial name search
    with db() as conn:
        cur = conn.execute(
            """
            SELECT user_id, username, first_name, last_name
            FROM users
            WHERE (first_name || ' ' || COALESCE(last_name,'')) LIKE ? OR COALESCE(username,'') LIKE ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (f"%{q}%", f"%{q}%"),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return int(rows[0][0])
    return rows  # multiple

def main_keyboard(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📊 Saldo", callback_data="menu:saldo"),
         InlineKeyboardButton("📝 Dichiara ricarica", callback_data="decl:start")],
        [InlineKeyboardButton("💳 Wallet", callback_data="wallet:req"),
         InlineKeyboardButton("ℹ️ Help", callback_data="menu:help")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🧾 Pending", callback_data="menu:pending"),
                     InlineKeyboardButton("👛 Wallet pending", callback_data="menu:walletpending")])
        rows.append([InlineKeyboardButton("👥 Utenti", callback_data="menu:utenti")])
    return InlineKeyboardMarkup(rows)

# -------------------- COMMANDS --------------------

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_text(f"User id: {update.effective_user.id}", reply_markup=main_keyboard(update.effective_user.id))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "Comandi utente:",
        "• /start – attiva il bot",
        "• /saldo – mostra i saldi (slot + wallet)",
        "",
        "Comandi admin:",
        "• /whoami – chat id",
        "• /utenti [tutti|approvati|pending] [pagina] [cerca <termine>]",
        "• /pending – ricariche in attesa",
        "• /walletpending – richieste wallet in attesa",
        "• /export users | /export recharges [YYYY-MM-DD] [YYYY-MM-DD]",
        "• /saldo <utente> – saldo di un utente per id/@username/nome",
    ]
    await update.message.reply_text("\n".join(lines), reply_markup=main_keyboard(update.effective_user.id))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_text("Ciao! 👋", reply_markup=main_keyboard(update.effective_user.id))

async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    # Admin query: /saldo <target>
    if is_admin(update.effective_user.id):
        parts = (update.message.text or "").strip().split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() == "/saldo":
            target = resolve_user_identifier(parts[1])
            if isinstance(target, list):
                # multiple matches → ask to pick
                await ask_user_pick(update, target, "saldo")
                return
            elif isinstance(target, int):
                uid = target
            else:
                uid = update.effective_user.id
        else:
            uid = update.effective_user.id
    else:
        uid = update.effective_user.id

    balances = get_balances(uid)
    wallet = get_wallet_kwh(uid)
    txt = [
        f"👤 Utente: {uid}" + (" (admin)" if is_admin(uid) else ""),
        "",
        "🔌 Slot:",
        f"• 8 kW: {fmt_kwh(balances[8])} kWh",
        f"• 3 kW: {fmt_kwh(balances[3])} kWh",
        f"• 5 kW: {fmt_kwh(balances[5])} kWh",
        "",
        f"👛 Wallet: {fmt_kwh(wallet)} kWh",
    ]
    await update.message.reply_text("\n".join(txt), reply_markup=main_keyboard(update.effective_user.id))

# -------------------- MENU CALLBACKS --------------------

async def on_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    data = q.data
    if data == "menu:saldo":
        balances = get_balances(uid); wallet = get_wallet_kwh(uid)
        await q.edit_message_text(
            "📊 *Saldo attuale*\n"
            f"• 8 kW: {fmt_kwh(balances[8])} kWh\n"
            f"• 3 kW: {fmt_kwh(balances[3])} kWh\n"
            f"• 5 kW: {fmt_kwh(balances[5])} kWh\n\n"
            f"👛 Wallet: {fmt_kwh(wallet)} kWh",
            reply_markup=main_keyboard(uid), parse_mode="Markdown"
        )
        return

    if data == "menu:pending":
        if not is_admin(uid):
            await q.answer("Solo admin", show_alert=True); return
        dummy = Update.de_json(q.to_dict(), context.application.bot)
        await pending(dummy, context)
        return

    if data == "menu:walletpending":
        if not is_admin(uid):
            await q.answer("Solo admin", show_alert=True); return
        dummy = Update.de_json(q.to_dict(), context.application.bot)
        await wallet_pending(dummy, context)
        return

    if data == "menu:utenti":
        if not is_admin(uid):
            await q.answer("Solo admin", show_alert=True); return
        context.args = []  # pagina 1, tutti
        await utenti(Update.de_json(q.to_dict(), context.application.bot), context)
        return

    if data == "menu:help":
        await q.edit_message_text(
            "Usa i pulsanti o i comandi /help, /saldo, /walletpending, /pending, /utenti, /export.",
            reply_markup=main_keyboard(uid)
        )
        return

# -------------------- PENDING RICARICHE (ADMIN) --------------------

async def ask_user_pick(update: Update, rows, tag: str):
    buttons = []
    for uid, username, fn, ln in rows:
        name = (fn or "") + (" " + ln if ln else "")
        who = f"@{username}" if username else (name.strip() or str(uid))
        buttons.append([InlineKeyboardButton(f"{who} (id {uid})", callback_data=f"userpick:{tag}:{uid}")])
    await update.message.reply_text("Seleziona utente:", reply_markup=InlineKeyboardMarkup(buttons))

async def render_pending_card(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    ids = context.user_data.get("pending_ids") or []
    if not ids:
        msg = "Nessuna ricarica in attesa."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg); return
        else:
            await update.message.reply_text(msg); return
    idx = max(0, min(idx, len(ids)-1))
    rid = ids[idx]
    context.user_data["pending_idx"] = idx

    with db() as conn:
        cur = conn.execute("SELECT id, user_id, slot, kwh, status, note, photo_file_id, created_at FROM recharges WHERE id=?", (rid,))
        r = cur.fetchone()
    if not r:
        await update.message.reply_text("Elemento non trovato."); return

    rid, uid, slot, kwh, status, note, photo, created = r
    caption = (
        f"🧾 Ricarica #{rid}\n"
        f"Utente: {uid}\n"
        f"Slot: {slot}\n"
        f"KWh: {fmt_kwh(kwh)}\n"
        f"Stato: {status}\n"
        f"Data: {created}\n"
        + (f"Nota: {note}\n" if note else "")
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Prev", callback_data="pending:nav:prev"),
         InlineKeyboardButton("➡️ Next", callback_data="pending:nav:next")],
        [InlineKeyboardButton("📸 Foto", callback_data=f"pending:photo:{rid}"),
         InlineKeyboardButton("ℹ️ Info", callback_data=f"pending:info:{rid}")],
        [InlineKeyboardButton("✅ Approva", callback_data=f"approve:{rid}"),
         InlineKeyboardButton("❌ Rifiuta", callback_data=f"reject:{rid}")],
    ])

    if update.callback_query:
        try:
            if photo:
                await update.callback_query.edit_message_caption(caption=caption, reply_markup=kb)
            else:
                await update.callback_query.edit_message_text(caption, reply_markup=kb)
        except Exception:
            await update.callback_query.edit_message_text(caption, reply_markup=kb)
    else:
        if photo:
            await update.message.reply_photo(photo=photo, caption=caption, reply_markup=kb)
        else:
            await update.message.reply_text(caption, reply_markup=kb)

async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with db() as conn:
        cur = conn.execute("SELECT id FROM recharges WHERE status='pending' ORDER BY id DESC")
        ids = [r[0] for r in cur.fetchall()]
    if not ids:
        await update.message.reply_text("Nessuna ricarica in attesa."); return
    context.user_data["pending_ids"] = ids
    context.user_data["pending_idx"] = 0
    await render_pending_card(update, context, 0)

async def on_pending_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Solo admin", show_alert=True); return

    data = q.data
    if data.startswith("pending:nav:"):
        direction = data.split(":")[-1]
        idx = int(context.user_data.get("pending_idx", 0))
        idx = idx + 1 if direction == "next" else idx - 1
        await render_pending_card(update, context, idx)
        return

    try:
        _, kind, rid_s = data.split(":", 2)
        rid = int(rid_s)
    except Exception:
        await q.answer("Callback non valida", show_alert=True); return

    if kind == "photo":
        with db() as conn:
            cur = conn.execute("SELECT photo_file_id FROM recharges WHERE id=?", (rid,))
            r = cur.fetchone()
        if not r or not r[0]:
            await q.answer("Nessuna foto", show_alert=True); return
        await context.bot.send_photo(chat_id=q.message.chat_id, photo=r[0], caption=f"Foto ricarica #{rid}")
        return

    if kind == "info":
        with db() as conn:
            cur = conn.execute("SELECT id, user_id, slot, kwh, status, note, created_at FROM recharges WHERE id=?", (rid,))
            r = cur.fetchone()
        if not r:
            await q.edit_message_text("Ricarica non trovata."); return
        _id, uid, slot, kwh, status, note, created = r
        await q.edit_message_text(
            f"🧾 Ricarica #{_id}\nUtente: {uid}\nSlot: {slot}\nKWh: {fmt_kwh(kwh)}\nStato: {status}\nData: {created}"
            + (f"\nNota: {note}" if note else "")
        )
        return

# Approva / Rifiuta
async def on_recharge_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Solo admin", show_alert=True); return
    data = q.data
    try:
        action, rid_s = data.split(":")
        rid = int(rid_s)
    except Exception:
        await q.answer("Callback non valida", show_alert=True); return

    with db() as conn:
        cur = conn.execute("SELECT id, user_id, slot, kwh, status FROM recharges WHERE id=?", (rid,))
        row = cur.fetchone()
        if not row:
            await q.edit_message_text("Ricarica non trovata."); return
        _id, uid, slot, kwh, status = row
        if status != "pending":
            await q.edit_message_text(f"Ricarica #{rid} già {status}."); return

        if action == "approve":
            # accredita kWh sullo slot
            if slot == 8:
                conn.execute("UPDATE users SET balance_slot8 = COALESCE(balance_slot8,0) + ? WHERE user_id=?", (str(kwh), uid))
            elif slot == 3:
                conn.execute("UPDATE users SET balance_slot3 = COALESCE(balance_slot3,0) + ? WHERE user_id=?", (str(kwh), uid))
            elif slot == 5:
                conn.execute("UPDATE users SET balance_slot5 = COALESCE(balance_slot5,0) + ? WHERE user_id=?", (str(kwh), uid))
            conn.execute(
                "UPDATE recharges SET status='approved', reviewed_at=datetime('now'), reviewer_id=? WHERE id=?",
                (q.from_user.id, rid)
            )
            msg = f"✅ Ricarica #{rid} approvata. Accreditati {fmt_kwh(kwh)} kWh su slot {slot}."
        else:
            conn.execute(
                "UPDATE recharges SET status='rejected', reviewed_at=datetime('now'), reviewer_id=? WHERE id=?",
                (q.from_user.id, rid)
            )
            msg = f"❌ Ricarica #{rid} rifiutata."

    await q.edit_message_text(msg)
    # avvisa utente
    try:
        await context.bot.send_message(chat_id=uid, text=msg)
    except Exception:
        pass

# -------------------- WALLET --------------------

async def on_wallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "wallet:req":
        context.user_data["awaiting_wallet_amount"] = True
        await q.edit_message_text("Inserisci l'importo € per ricaricare il wallet (es. 20 o 20.5):")
        return

async def on_message_amount_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_wallet_amount"):
        return
    txt = (update.message.text or "").replace(",", ".").strip()
    try:
        amt = Decimal(txt)
        if amt <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Importo non valido. Inserisci un numero positivo (es. 12.5).")
        return

    context.user_data["awaiting_wallet_amount"] = False
    with db() as conn:
        conn.execute("INSERT INTO wallet_requests (user_id, amount_eur) VALUES (?,?)", (update.effective_user.id, str(amt)))
        cur = conn.execute("SELECT last_insert_rowid()")
        wid = cur.fetchone()[0]

    await update.message.reply_text(f"Richiesta inviata 👌 (id #{wid}). Gli admin la valuteranno.")

    # notifica admin
    if ADMIN_IDS:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Accetta", callback_data=f"wallet:accept:{wid}"),
             InlineKeyboardButton("❌ Rifiuta", callback_data=f"wallet:reject:{wid}")],
        ])
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=aid,
                    text=f"👛 Nuova richiesta wallet #{wid}\nUtente: {update.effective_user.id}\nImporto: € {amt}",
                    reply_markup=kb
                )
            except Exception as e:
                log.warning("Notify admin wallet failed %s: %s", aid, e)

async def wallet_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with db() as conn:
        cur = conn.execute("SELECT id FROM wallet_requests WHERE status='pending' ORDER BY id DESC")
        ids = [r[0] for r in cur.fetchall()]
    if not ids:
        await update.message.reply_text("Nessuna richiesta wallet in attesa."); return
    context.user_data["wallet_ids"] = ids
    context.user_data["wallet_idx"] = 0
    await render_wallet_card(update, context, 0)

async def render_wallet_card(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    ids = context.user_data.get("wallet_ids") or []
    if not ids:
        msg = "Nessuna richiesta wallet in attesa."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg); return
        else:
            await update.message.reply_text(msg); return
    idx = max(0, min(idx, len(ids)-1))
    wid = ids[idx]
    context.user_data["wallet_idx"] = idx

    with db() as conn:
        cur = conn.execute("SELECT id, user_id, amount_eur, status, created_at FROM wallet_requests WHERE id=?", (wid,))
        r = cur.fetchone()
    if not r:
        await update.message.reply_text("Elemento non trovato."); return

    wid, uid, eur, status, created = r
    caption = (
        f"👛 Wallet request #{wid}\n"
        f"Utente: {uid}\n"
        f"Importo: € {eur}\n"
        f"Stato: {status}\n"
        f"Data: {created}\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Prev", callback_data="wallet:nav:prev"),
         InlineKeyboardButton("➡️ Next", callback_data="wallet:nav:next")],
        [InlineKeyboardButton("✅ Accetta", callback_data=f"wallet:accept:{wid}"),
         InlineKeyboardButton("❌ Rifiuta", callback_data=f"wallet:reject:{wid}")],
    ])

    if update.callback_query:
        await update.callback_query.edit_message_text(caption, reply_markup=kb)
    else:
        await update.message.reply_text(caption, reply_markup=kb)

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
            conn.execute(
                "UPDATE wallet_requests SET status='rejected', reviewed_at=datetime('now'), reviewer_id=? WHERE id=?",
                (q.from_user.id, wid),
            )
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
        await update.message.reply_text("Valore non valido. Inserisci kWh positivi (es. 15 o 12.5).")
        return

    context.user_data["awaiting_wallet_kwh_for"] = None
    with db() as conn:
        # accredito wallet_kwh e segno richiesta come approvata
        cur = conn.execute("SELECT user_id FROM wallet_requests WHERE id=?", (wid,))
        r = cur.fetchone()
        if not r:
            await update.message.reply_text("Richiesta non trovata."); return
        uid = int(r[0])
        conn.execute("UPDATE users SET wallet_kwh = COALESCE(wallet_kwh,0) + ? WHERE user_id=?", (str(kwh), uid))
        conn.execute("UPDATE wallet_requests SET status='approved', reviewed_at=datetime('now'), reviewer_id=? WHERE id=?", (update.effective_user.id, wid))

    await update.message.reply_text(f"✅ Accreditati {fmt_kwh(kwh)} kWh nel wallet dell'utente {uid}.")
    try:
        await context.bot.send_message(chat_id=uid, text=f"👛 Il tuo wallet è stato ricaricato di {fmt_kwh(kwh)} kWh (richiesta #{wid}).")
    except Exception:
        pass

# -------------------- DICHIARAZIONE RICARICA (utente) --------------------

async def on_decl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if q.data == "decl:start":
        context.user_data["decl_await_kwh"] = True
        context.user_data["decl_kwh"] = None
        context.user_data["decl_photo_id"] = None
        context.user_data["decl_note"] = ""
        await q.edit_message_text("Inserisci i kWh ricaricati (es. 12.5):")
        return

    if q.data.startswith("decl:slot:"):
        try:
            slot = int(q.data.split(":")[-1])
            if slot not in (8,3,5):
                raise ValueError
        except Exception:
            await q.answer("Slot non valido", show_alert=True); return

        context.user_data["decl_slot"] = slot
        # salva ricarica pending
        with db() as conn:
            conn.execute(
                "INSERT INTO recharges (user_id, slot, kwh, status, note, photo_file_id) VALUES (?,?,?,?,?,?)",
                (uid, slot, str(context.user_data.get("decl_kwh")), "pending", context.user_data.get("decl_note",""), context.user_data.get("decl_photo_id"))
            )
            cur = conn.execute("SELECT last_insert_rowid()"); rid = cur.fetchone()[0]
        await q.edit_message_text(f"Ricarica inviata ✅ (id #{rid}). Gli admin la valuteranno.")
        # notifica admin
        if ADMIN_IDS:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Approva", callback_data=f"approve:{rid}"),
                 InlineKeyboardButton("❌ Rifiuta", callback_data=f"reject:{rid}")]
            ])
            photo_id = context.user_data.get("decl_photo_id")
            for aid in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=aid,
                        text=f"📝 Nuova ricarica dichiarata #{rid}\nUtente: {uid}\nSlot: {slot}\nKWh: {fmt_kwh(context.user_data['decl_kwh'])}"
                             + (f"\nNota: {context.user_data.get('decl_note')}" if context.user_data.get("decl_note") else ""),
                        reply_markup=kb
                    )
                    if photo_id:
                        await context.bot.send_photo(chat_id=aid, photo=photo_id, caption=f"Ricevuta ricarica #{rid}")
                except Exception as e:
                    log.warning("Notify admin recharge failed %s: %s", aid, e)
        # pulizia stato
        context.user_data["decl_kwh"] = None
        context.user_data["decl_photo_id"] = None
        context.user_data["decl_note"] = ""
        return

async def on_message_decl_kwh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("decl_await_kwh"):
        return
    txt = (update.message.text or "").replace(",", ".").strip()
    try:
        kwh = Decimal(txt)
        if kwh <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Valore non valido. Inserisci un numero positivo (es. 12.5).")
        return

    context.user_data["decl_kwh"] = kwh
    context.user_data["decl_await_kwh"] = False
    context.user_data["decl_await_photo"] = True
    await update.message.reply_text("Ok 👍\nOra invia **una foto** della ricevuta (obbligatoria).")

async def on_message_decl_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("decl_await_photo"):
        return
    if not update.message.photo:
        await update.message.reply_text("Devi inviare **una foto** della ricevuta.")
        return
    file_id = update.message.photo[-1].file_id
    context.user_data["decl_photo_id"] = file_id
    context.user_data["decl_await_photo"] = False
    context.user_data["decl_await_note"] = True
    await update.message.reply_text("Foto ricevuta 📷\nAggiungi una **nota** (opzionale), oppure scrivi **ok** per inviare.")

async def on_message_decl_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("decl_await_note"):
        return
    note = (update.message.text or "").strip()
    context.user_data["decl_note"] = "" if note.lower() == "ok" else note

    # scegli slot
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("8 kW", callback_data="decl:slot:8"),
         InlineKeyboardButton("3 kW", callback_data="decl:slot:3"),
         InlineKeyboardButton("5 kW", callback_data="decl:slot:5")],
    ])
    await update.message.reply_text(
        f"Conferma:\nKWh: {fmt_kwh(context.user_data['decl_kwh'])}"
        + (f"\nNota: {context.user_data['decl_note']}" if context.user_data['decl_note'] else "")
        + "\n\nSeleziona lo *slot*:",
        reply_markup=kb, parse_mode="Markdown"
    )
    context.user_data["decl_await_note"] = False

# -------------------- USERS LIST + DELETE --------------------

PAGE_SIZE_USERS = 20

async def utenti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args or []
    status = "tutti"
    page = 1
    query = ""

    i = 0
    if i < len(args) and args[i].lower() in {"tutti","approvati","pending"}:
        status = args[i].lower(); i += 1
    if i < len(args):
        try:
            page = max(1, int(args[i])); i += 1
        except Exception:
            pass
    if i < len(args) and args[i].lower() == "cerca" and (i+1) < len(args):
        query = " ".join(args[i+1:])

    where = []
    params: List = []
    if status == "approvati":
        where.append("approved=1")
    elif status == "pending":
        where.append("approved=0")
    if query:
        where.append("(COALESCE(username,'') LIKE ? OR first_name LIKE ? OR last_name LIKE ?)")
        params += [f"%{query}%", f"%{query}%", f"%{query}%"]
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    offset = (page-1) * PAGE_SIZE_USERS

    with db() as conn:
        cur = conn.execute(f"SELECT COUNT(*) FROM users {where_sql}", params)
        total = cur.fetchone()[0]
        cur = conn.execute(
            f"""
            SELECT user_id, username, first_name, last_name, approved, balance_slot8, balance_slot3, balance_slot5, wallet_kwh, created_at
            FROM users
            {where_sql}
            ORDER BY created_at DESC
            LIMIT {PAGE_SIZE_USERS} OFFSET {offset}
            """,
            params
        )
        rows = cur.fetchall()

    lines = [f"👥 Utenti – {status} – pagina {page}"]
    for (uid, username, fn, ln, approved, b8, b3, b5, w, created) in rows:
        name = (fn or "") + (" " + ln if ln else "")
        lines.append(
            f"• {uid} {'✅' if approved else '⏳'} {('@'+username) if username else name or ''} – "
            f"8kW {fmt_kwh(b8)} | 3kW {fmt_kwh(b3)} | 5kW {fmt_kwh(b5)} | 👛 {fmt_kwh(w)}"
        )
    nav = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Prev", callback_data=f"users:page:{status}:{max(1,page-1)}:{query or '-'}"),
         InlineKeyboardButton("➡️ Next", callback_data=f"users:page:{status}:{page+1}:{query or '-'}")],
        [InlineKeyboardButton("🗑️ Elimina utente", callback_data="users:delete:start")]
    ])
    await update.message.reply_text("\n".join(lines) + f"\n\nTotale: {total}", reply_markup=nav)

async def on_users_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Solo admin", show_alert=True); return
    try:
        _, _, status, page_s, query = q.data.split(":", 4)
        page = int(page_s)
        if query == "-":
            query = ""
    except Exception:
        await q.answer("Nav errata", show_alert=True); return
    context.args = [status, str(page)] + (["cerca", query] if query else [])
    await utenti(Update.de_json(q.to_dict(), context.application.bot), context)

async def delete_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Solo admin", show_alert=True); return
    context.user_data["awaiting_delete_user"] = True
    await q.edit_message_text("Invia l'ID/@username/nome utente da eliminare. (Operazione irreversibile)")

async def on_userpick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, tag, uid_s = q.data.split(":", 2)
        uid = int(uid_s)
    except Exception:
        await q.answer("Selezione non valida", show_alert=True); return
    if tag == "saldo":
        dummy = Update.de_json(q.to_dict(), context.application.bot)
        dummy.effective_user.id = uid  # for display only
        balances = get_balances(uid); wallet = get_wallet_kwh(uid)
        await q.edit_message_text(
            "📊 *Saldo attuale*\n"
            f"• 8 kW: {fmt_kwh(balances[8])} kWh\n"
            f"• 3 kW: {fmt_kwh(balances[3])} kWh\n"
            f"• 5 kW: {fmt_kwh(balances[5])} kWh\n\n"
            f"👛 Wallet: {fmt_kwh(wallet)} kWh",
            parse_mode="Markdown"
        )
    elif tag == "delete":
        context.user_data["awaiting_delete_user"] = False
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Conferma", callback_data=f"userdel:yes:{uid}"),
             InlineKeyboardButton("❌ Annulla", callback_data=f"userdel:no:{uid}")]
        ])
        await q.edit_message_text(f"Confermi l'eliminazione dell'utente {uid}?", reply_markup=kb)

async def on_userdel_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Solo admin", show_alert=True); return
    try:
        _, yesno, uid_s = q.data.split(":")
        uid = int(uid_s)
    except Exception:
        await q.answer("Callback non valida", show_alert=True); return
    if yesno == "no":
        await q.edit_message_text("Annullato."); return
    with db() as conn:
        conn.execute("DELETE FROM users WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM recharges WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM wallet_requests WHERE user_id=?", (uid,))
    await q.edit_message_text(f"Utente {uid} eliminato ✅")

async def on_message_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_delete_user"):
        return
    q = (update.message.text or "").strip()
    target = resolve_user_identifier(q)
    if isinstance(target, list):
        await ask_user_pick(update, target, "delete")
        return
    if not isinstance(target, int):
        await update.message.reply_text("Nessun utente trovato."); return
    uid = int(target)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Conferma", callback_data=f"userdel:yes:{uid}"),
         InlineKeyboardButton("❌ Annulla", callback_data=f"userdel:no:{uid}")]
    ])
    context.user_data["awaiting_delete_user"] = False
    await update.message.reply_text(f"Confermi l'eliminazione dell'utente {uid}?", reply_markup=kb)

# -------------------- EXPORT --------------------

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Uso: /export users | /export recharges [YYYY-MM-DD] [YYYY-MM-DD]"); return
    what = args[0].lower()
    import csv, io
    if what == "users":
        with db() as conn:
            cur = conn.execute(
                """
                SELECT user_id, username, first_name, last_name, balance_slot8, balance_slot3, balance_slot5, wallet_kwh, approved, created_at
                FROM users
                ORDER BY user_id ASC
                """
            )
            rows = cur.fetchall()
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow(["user_id","username","first_name","last_name","balance_slot8","balance_slot3","balance_slot5","wallet_kwh","approved","created_at"])
        for r in rows: w.writerow(list(r))
        buf.seek(0)
        await update.message.reply_document(document=("users.csv", buf.getvalue().encode("utf-8")), filename="users.csv", caption="Export utenti")
        return

    if what == "recharges":
        date_from = None; date_to = None
        if len(args) >= 2: date_from = args[1]
        if len(args) >= 3: date_to = args[2]
        where = []; params = []
        if date_from:
            where.append("DATE(created_at) >= DATE(?)"); params.append(date_from)
        if date_to:
            where.append("DATE(created_at) <= DATE(?)"); params.append(date_to)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        with db() as conn:
            cur = conn.execute(
                f"""
                SELECT id, user_id, slot, kwh, status, note, created_at, reviewed_at, reviewer_id
                FROM recharges
                {where_sql}
                ORDER BY id ASC
                """, params
            )
            rows = cur.fetchall()
        import csv, io
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow(["id","user_id","slot","kwh","status","note","created_at","reviewed_at","reviewer_id"])
        for r in rows: w.writerow(list(r))
        buf.seek(0)
        await update.message.reply_document(document=("recharges.csv", buf.getvalue().encode("utf-8")), filename="recharges.csv", caption="Export ricariche")
        return

    await update.message.reply_text("Tipo export non valido.")

# -------------------- STARTUP / ERROR --------------------

async def startup_notify(app: Application):
    try:
        if ADMIN_IDS:
            for aid in ADMIN_IDS:
                try:
                    await app.bot.send_message(chat_id=aid, text=f"✅ Bot avviato su {os.getenv('RAILWAY_SERVICE_NAME','local')}\nPython {sys.version.split()[0]} • PTB {telegram.__version__}")
                except Exception as e:
                    print("[BOOT] notify admin failed:", aid, e)
        try:
            app.job_queue.run_repeating(daily_ping, interval=timedelta(hours=24), first=timedelta(hours=24))
            print("[PING] scheduled every 24h")
        except Exception as e:
            print("[PING] schedule failed:", e)
    except Exception as e:
        print("[BOOT] startup_notify error:", e)

async def daily_ping(context: ContextTypes.DEFAULT_TYPE):
    try:
        if not ADMIN_IDS: return
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=aid, text=f"🏓 Ping giornaliero: bot attivo\n{ts}")
            except Exception as e:
                print("[PING] notify admin failed:", aid, e)
    except Exception as e:
        print("[PING] error:", e)

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("[ERROR] Unhandled exception", exc_info=context.error)

# -------------------- FACTORY --------------------

def create_application():
    if not TOKEN:
        raise RuntimeError('TELEGRAM_TOKEN non impostato')
    migrate()
    app = Application.builder().token(TOKEN).post_init(startup_notify).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("saldo", saldo))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("walletpending", wallet_pending))
    app.add_handler(CommandHandler("utenti", utenti))
    app.add_handler(CommandHandler("export", export))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_main_menu, pattern=r"^menu:(saldo|pending|walletpending|utenti|help)$"))
    app.add_handler(CallbackQueryHandler(on_pending_action, pattern=r"^pending:(photo|info):\d+$"))
    app.add_handler(CallbackQueryHandler(on_pending_action, pattern=r"^pending:nav:(prev|next)$"))
    app.add_handler(CallbackQueryHandler(on_wallet_callback, pattern=r"^wallet:(req)$"))
    app.add_handler(CallbackQueryHandler(on_wallet_admin, pattern=r"^wallet:(nav:(prev|next)|accept:\d+|reject:\d+)$"))
    app.add_handler(CallbackQueryHandler(on_users_nav, pattern=r"^users:page:"))
    app.add_handler(CallbackQueryHandler(delete_user_start, pattern=r"^users:delete:start$"))
    app.add_handler(CallbackQueryHandler(on_userpick, pattern=r"^userpick:(saldo|delete):\d+$"))
    app.add_handler(CallbackQueryHandler(on_userdel_confirm, pattern=r"^userdel:(yes|no):\d+$"))
    app.add_handler(CallbackQueryHandler(on_decl_callback, pattern=r"^decl:(start|slot:(8|3|5))$"))
    app.add_handler(CallbackQueryHandler(on_recharge_action, pattern=r"^(approve|reject):\d+$"))

    # Messages — ordine IMPORTANTE:
    app.add_handler(MessageHandler(filters.PHOTO, on_message_decl_photo))                       # foto per dichiarazione
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_decl_kwh))      # kwh per dichiarazione
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_decl_note))     # nota per dichiarazione

    # wallet (utente) / wallet kWh (admin) / delete user
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_amount_wallet))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_admin_wallet_kwh))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_delete_user))

    # Error handler
    app.add_error_handler(on_error)
    return app

# -------------------- MAIN --------------------

def main():
    if not TOKEN:
        log.error("TELEGRAM_TOKEN non impostato")
        sys.exit(1)

    app = create_application()

    log.info("Bot in avvio...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
