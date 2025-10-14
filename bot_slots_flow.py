#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
saldo-bot
- PTB v20 async
- SQLite persistence
- Features:
  * /start /help /whoami
  * /saldo (user) + /saldo <utente> (admin) — show slot 8/3/5 + wallet kWh
  * Menu principale: 📊 Saldo, 📝 Dichiara ricarica, 💳 Wallet, ℹ️ Help (+ admin: 🧾 Pending, 👛 Wallet pending, 👥 Utenti)
  * Slots keyboard shows balances; main keyboard shows Wallet • X kWh
  * Rate limit 60s su dichiarazione utente
  * /pending con paginazione + photo/info + approve/reject
  * /utenti con paginazione + elimina utente (conferma) + ricerca
  * /export users/recharges
  * Wallet top-up: utente richiede € → admin /walletpending approva (inserendo kWh) o rifiuta → utente notificato
  * Startup logs + notifica avvio agli admin + ping giornaliero ogni 24h
"""

import os
import sys
import sqlite3
import logging
import time
from typing import Optional, Dict, Any, List, Tuple
from decimal import Decimal, ROUND_HALF_UP
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

# -------------------- CONFIG --------------------

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

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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
                status TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected
                note TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                reviewed_at TEXT,
                reviewer_id INTEGER
            );
            """
        )

def ensure_user(u: telegram.User):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, approved)
            VALUES (?, ?, ?, ?, COALESCE((SELECT approved FROM users WHERE user_id=?), 0))
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name
            """,
            (u.id, u.username, u.first_name, u.last_name, u.id),
        )

# -------------------- HELPERS --------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

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
            """
            SELECT user_id, COALESCE(username,''), COALESCE(first_name,''), COALESCE(last_name,'')
            FROM users
            WHERE (IFNULL(username,'') LIKE ? OR IFNULL(first_name,'') LIKE ? OR IFNULL(last_name,'') LIKE ?)
            ORDER BY user_id ASC
            LIMIT 10
            """,
            (like, like, like),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0][0]
    return [(r[0], r[1], r[2], r[3]) for r in rows]

# Rate limit (user_id, key) -> timestamp
RATE_LIMIT: Dict[Tuple[int,str], int] = {}
def check_rate_limit(user_id: int, key: str, window: int) -> int:
    now = int(time.time())
    last = RATE_LIMIT.get((user_id, key), 0)
    if now - last < window:
        return window - (now - last)
    RATE_LIMIT[(user_id, key)] = now
    return 0

# -------------------- KEYBOARDS --------------------

def main_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    wlabel = "💳 Ricarica wallet"
    isadm = False
    if user_id is not None:
        try:
            wlabel = f"💳 Wallet • {fmt_kwh(get_wallet_kwh(user_id))} kWh"
        except Exception:
            pass
        isadm = is_admin(user_id)

    buttons = [
        [InlineKeyboardButton("📊 Saldo", callback_data="menu:saldo")],
        [InlineKeyboardButton("📝 Dichiara ricarica", callback_data="decl:start")],
        [InlineKeyboardButton(wlabel, callback_data="wallet:req")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="menu:help")],
    ]
    if isadm:
        buttons += [
            [InlineKeyboardButton("🧾 Pending", callback_data="menu:pending"),
             InlineKeyboardButton("👛 Wallet pending", callback_data="menu:walletpending")],
            [InlineKeyboardButton("👥 Utenti", callback_data="menu:utenti")],
        ]
    return InlineKeyboardMarkup(buttons)

def slots_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    try:
        b = get_balances(user_id) if user_id else None
    except Exception:
        b = None
    def label(slot):
        if b and slot in b:
            return f"Slot {slot} • {fmt_kwh(b[slot])} kWh"
        return f"Slot {slot}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label(8), callback_data="slot:8")],
        [InlineKeyboardButton(label(3), callback_data="slot:3")],
        [InlineKeyboardButton(label(5), callback_data="slot:5")],
        [InlineKeyboardButton("Annulla", callback_data="flow:cancel")],
    ])

async def ask_user_pick(update: Update, rows, tag: str):
    buttons = []
    for uid, username, fn, ln in rows:
        name = (fn or "") + (" " + ln if ln else "")
        who = f"@{username}" if username else (name.strip() or str(uid))
        buttons.append([InlineKeyboardButton(f"{who} (id {uid})", callback_data=f"userpick:{tag}:{uid}")])
    await update.message.reply_text("Seleziona utente:", reply_markup=InlineKeyboardMarkup(buttons))

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
            if target is None:
                await update.message.reply_text("Utente non trovato.")
                return
            if isinstance(target, list):
                await ask_user_pick(update, target, "saldo"); return
            oid = int(target)
            b = get_balances(oid)
            w = get_wallet_kwh(oid)
            await update.message.reply_text(
                "Saldi utente {oid}:\n"
                f"• Slot 8: {fmt_kwh(b[8])} kWh\n"
                f"• Slot 3: {fmt_kwh(b[3])} kWh\n"
                f"• Slot 5: {fmt_kwh(b[5])} kWh\n"
                f"• Wallet: {fmt_kwh(w)} kWh".format(oid=oid),
                reply_markup=main_keyboard(update.effective_user.id)
            )
            return
    # Self
    u = update.effective_user
    b = get_balances(u.id)
    w = get_wallet_kwh(u.id)
    await update.message.reply_text(
        "I tuoi saldi:\n"
        f"• Slot 8: {fmt_kwh(b[8])} kWh\n"
        f"• Slot 3: {fmt_kwh(b[3])} kWh\n"
        f"• Slot 5: {fmt_kwh(b[5])} kWh\n"
        f"• Wallet: {fmt_kwh(w)} kWh",
        reply_markup=main_keyboard(u.id)
    )

# -------------------- MENU CALLBACK --------------------

async def on_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "menu:saldo":
        b = get_balances(uid)
        w = get_wallet_kwh(uid)
        txt = (
            "I tuoi saldi:\n"
            f"• Slot 8: {fmt_kwh(b[8])} kWh\n"
            f"• Slot 3: {fmt_kwh(b[3])} kWh\n"
            f"• Slot 5: {fmt_kwh(b[5])} kWh\n"
            f"• Wallet: {fmt_kwh(w)} kWh"
        )
        await q.edit_message_text(txt, reply_markup=main_keyboard(uid))
        return

    if data == "menu:pending":
        if not is_admin(uid):
            await q.answer("Solo admin", show_alert=True); return
        await pending(Update.de_json(q.to_dict(), context.application.bot), context)
        return

    if data == "menu:walletpending":
        if not is_admin(uid):
            await q.answer("Solo admin", show_alert=True); return
        await wallet_pending(Update.de_json(q.to_dict(), context.application.bot), context)
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

async def render_pending_card(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    ids = context.user_data.get("pending_ids") or []
    if not ids:
        msg = "Nessuna ricarica in attesa."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg); return
        await update.message.reply_text(msg); return
    idx = max(0, min(idx, len(ids)-1))
    context.user_data["pending_idx"] = idx
    rid = ids[idx]
    with db() as conn:
        cur = conn.execute(
            """
            SELECT r.id, r.user_id, r.slot, r.kwh, r.status, COALESCE(r.created_at,''), COALESCE(r.note,''),
                   COALESCE(r.photo_file_id,''), COALESCE(u.username,''), COALESCE(u.first_name,''), COALESCE(u.last_name,'')
            FROM recharges r
            LEFT JOIN users u ON u.user_id = r.user_id
            WHERE r.id = ?
            """,
            (rid,)
        )
        row = cur.fetchone()
    if not row:
        await update.effective_message.reply_text(f"Ricarica #{rid} non trovata."); return
    (_rid, uid, slot, kwh, status, created_at, note, photo_id, username, fn, ln) = row
    who = f"@{username}" if username else ((fn or "") + (" " + ln if ln else "")).strip() or str(uid)
    created_fmt = (created_at or "-").replace("T"," ")[:19]
    text = (
        f"[{idx+1}/{len(ids)}] Ricarica #{_rid} • {created_fmt}\n"
        f"Utente: {who} (id {uid})\n"
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
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        except Exception:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)

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

    with db() as conn:
        cur = conn.execute(
            """
            SELECT r.id, r.user_id, r.slot, r.kwh, r.status, COALESCE(r.created_at,''),
                   COALESCE(r.reviewed_at,''), COALESCE(r.reviewer_id,''), COALESCE(r.note,''), COALESCE(r.photo_file_id,''),
                   COALESCE(u.username,''), COALESCE(u.first_name,''), COALESCE(u.last_name,'')
            FROM recharges r
            LEFT JOIN users u ON u.user_id = r.user_id
            WHERE r.id = ?
            """,
            (rid,)
        )
        row = cur.fetchone()
    if not row:
        await q.edit_message_text("Ricarica non trovata (forse già gestita)."); return
    (_rid, uid, slot, kwh, status, created_at, reviewed_at, reviewer_id, note, photo_id, username, fn, ln) = row

    if kind == "photo":
        if photo_id:
            await context.bot.send_photo(chat_id=q.message.chat.id, photo=photo_id, caption=f"#{rid} – slot {slot}, {fmt_kwh(Decimal(str(kwh)))} kWh")
        else:
            await q.answer("Nessuna foto.", show_alert=True)
        return
    if kind == "info":
        who = f"@{username}" if username else ((fn or "") + (" " + ln if ln else "")).strip() or str(uid)
        created_fmt = (created_at or "-").replace("T"," ")[:19]
        reviewed_fmt = (reviewed_at or "-").replace("T"," ")[:19]
        info = [
            f"Ricarica #{rid}",
            f"Utente: {who} (id {uid})",
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
            [InlineKeyboardButton("✅ Approva", callback_data=f"approve:{rid}"),
             InlineKeyboardButton("❌ Rifiuta", callback_data=f"reject:{rid}")],
        ])
        try:
            await q.edit_message_text("\n".join(info), reply_markup=kb)
        except Exception:
            await context.bot.send_message(chat_id=q.message.chat.id, text="\n".join(info), reply_markup=kb)
        return

# -------------------- WALLET REQUEST FLOW --------------------

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
        conn.execute("INSERT INTO wallet_requests (user_id, amount_eur, status) VALUES (?,?, 'pending')", (update.effective_user.id, str(amt)))
    context.user_data["awaiting_wallet_amount_eur"] = False
    await update.message.reply_text(f"Richiesta inviata: € {amt}. Un admin valuterà la ricarica.")

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
        await update.effective_message.reply_text("Nessuna richiesta."); return
    idx = max(0, min(idx, len(ids)-1))
    context.user_data["wallet_idx"] = idx
    wid = ids[idx]
    with db() as conn:
        cur = conn.execute(
            """
            SELECT w.id, w.user_id, w.amount_eur, w.status, COALESCE(w.created_at,''), COALESCE(w.note,''),
                   COALESCE(u.username,''), COALESCE(u.first_name,''), COALESCE(u.last_name,''), COALESCE(u.wallet_kwh,0)
            FROM wallet_requests w
            LEFT JOIN users u ON u.user_id = w.user_id
            WHERE w.id = ?
            """,
            (wid,)
        )
        row = cur.fetchone()
    if not row:
        await update.effective_message.reply_text(f"Richiesta #{wid} non trovata."); return
    (_id, uid, amt, status, created_at, note, username, fn, ln, wallet_kwh) = row
    who = f"@{username}" if username else ((fn or "") + (" " + ln if ln else "")).strip() or str(uid)
    created_fmt = (created_at or "-").replace("T"," ")[:19]
    text = (
        f"[{idx+1}/{len(ids)}] Wallet #{_id} • {created_fmt}\n"
        f"Utente: {who} (id {uid})\n"
        f"Importo richiesto: € {amt}\n"
        f"Wallet attuale: {fmt_kwh(Decimal(str(wallet_kwh)))} kWh"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️", callback_data="wallet:nav:prev"),
         InlineKeyboardButton("➡️", callback_data="wallet:nav:next")],
        [InlineKeyboardButton("✅ Accetta", callback_data=f"wallet:accept:{_id}"),
         InlineKeyboardButton("❌ Rifiuta", callback_data=f"wallet:reject:{_id}")],
    ])
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        except Exception:
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
        await update.message.reply_text("Valore non valido. Inserisci un numero positivo (kWh).")
        return
    with db() as conn:
        cur = conn.execute("SELECT user_id FROM wallet_requests WHERE id=? AND status='pending'", (wid,))
        r = cur.fetchone()
        if not r:
            await update.message.reply_text("Richiesta non trovata o già processata.")
            context.user_data.pop("awaiting_wallet_kwh_for", None); return
        uid = r[0]
        conn.execute("UPDATE users SET wallet_kwh = COALESCE(wallet_kwh,0) + ? WHERE user_id=?", (str(kwh), uid))
        conn.execute(
            "UPDATE wallet_requests SET status='approved', reviewed_at=datetime('now'), reviewer_id=? WHERE id=?",
            (update.effective_user.id, wid),
        )
    context.user_data.pop("awaiting_wallet_kwh_for", None)
    await update.message.reply_text(f"Accreditati {fmt_kwh(kwh)} kWh sul wallet dell'utente {uid}.")
    try:
        await context.bot.send_message(chat_id=uid, text=f"La tua richiesta wallet è stata approvata. Accreditati {fmt_kwh(kwh)} kWh.")
    except Exception:
        pass

# -------------------- DICHIARA RICARICA (UTENTE) --------------------

async def on_decl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id

    # Annulla
    if data == "decl:cancel":
        for k in ["decl_slot","decl_kwh","decl_photo_id","decl_await_kwh","decl_await_photo","decl_await_note"]:
            context.user_data.pop(k, None)
        await q.edit_message_text("Operazione annullata.", reply_markup=main_keyboard(uid))
        return

    # Avvio: scegli slot
    if data == "decl:start":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔌 Slot 8", callback_data="decl:slot:8")],
            [InlineKeyboardButton("🔌 Slot 3", callback_data="decl:slot:3")],
            [InlineKeyboardButton("🔌 Slot 5", callback_data="decl:slot:5")],
            [InlineKeyboardButton("❌ Annulla", callback_data="decl:cancel")],
        ])
        await q.edit_message_text("Seleziona lo slot che hai ricaricato:", reply_markup=kb)
        return

    # Scelto uno slot
    if data.startswith("decl:slot:"):
        slot = int(data.split(":")[-1])
        context.user_data["decl_slot"] = slot
        context.user_data["decl_await_kwh"] = True
        await q.edit_message_text(
            f"Hai scelto Slot {slot}.\n"
            "Inserisci i kWh da dichiarare (es. 12.5).\n\n"
            "• Scrivi il numero\n• Oppure premi ❌ Annulla",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Annulla", callback_data="decl:cancel")]])
        )
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
        await update.message.reply_text(
            "Valore non valido. Inserisci un numero positivo (es. 12.5), oppure premi ❌ Annulla.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Annulla", callback_data="decl:cancel")]])
        )
        return

    context.user_data["decl_kwh"] = kwh
    context.user_data["decl_await_kwh"] = False
    context.user_data["decl_await_photo"] = True
    await update.message.reply_text(
        "Ok 👍\n\nOra puoi inviare **una foto** della ricevuta (opzionale).\n"
        "Se non hai foto, scrivi **salta**.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Salta foto", callback_data="decl:cancel")]])
    )

async def on_message_decl_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("decl_await_photo"):
        return
    # testo "salta"
    if update.message.text and update.message.text.strip().lower() == "salta":
        context.user_data["decl_await_photo"] = False
        context.user_data["decl_await_note"] = True
        await update.message.reply_text(
            "Vuoi aggiungere una **nota**? (testo libero)\nOppure scrivi **ok** per inviare.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Invia senza nota", callback_data="decl:cancel")]])
        )
        return
    # foto
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        context.user_data["decl_photo_id"] = file_id
        context.user_data["decl_await_photo"] = False
        context.user_data["decl_await_note"] = True
        await update.message.reply_text("Foto ricevuta 📷\nAggiungi una **nota** (opzionale), oppure scrivi **ok** per inviare.")
        return
    # altro
    await update.message.reply_text(
        "Invia una foto oppure scrivi **salta**.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Salta foto", callback_data="decl:cancel")]])
    )

async def on_message_decl_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("decl_await_note"):
        return
    txt = (update.message.text or "").strip()
    note = None if txt.lower() == "ok" else (txt or None)

    uid = update.effective_user.id
    slot = context.user_data.get("decl_slot")
    kwh  = context.user_data.get("decl_kwh")
    photo_id = context.user_data.get("decl_photo_id")

    if not slot or not kwh:
        for k in ["decl_slot","decl_kwh","decl_photo_id","decl_await_kwh","decl_await_photo","decl_await_note"]:
            context.user_data.pop(k, None)
        await update.message.reply_text("Qualcosa è andato storto, riprova.", reply_markup=main_keyboard(uid))
        return

    # Rate limit 60s
    rem = check_rate_limit(uid, "declare", 60)
    if rem > 0:
        await update.message.reply_text(
            f"Hai già inviato una dichiarazione da poco. Riprova tra {rem} secondi.",
            reply_markup=main_keyboard(uid)
        )
        return

    with db() as conn:
        conn.execute(
            "INSERT INTO recharges (user_id, slot, kwh, status, note, photo_file_id) VALUES (?,?,?,?,?,?)",
            (uid, int(slot), str(kwh), 'pending', note, photo_id)
        )

    for k in ["decl_slot","decl_kwh","decl_photo_id","decl_await_kwh","decl_await_photo","decl_await_note"]:
        context.user_data.pop(k, None)

    await update.message.reply_text(
        f"✅ Dichiarazione inviata.\n• Slot {slot}\n• kWh: {fmt_kwh(kwh)}\n"
        + (f"• Nota: {note}\n" if note else "")
        + "Un admin la valuterà a breve.",
        reply_markup=main_keyboard(uid)
    )

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
    if i < len(args) and args[i].lower() == "cerca" and i+1 < len(args):
        query = args[i+1]; i += 2

    where = ["1=1"]; params: List[Any] = []
    if status == "approvati":
        where.append("COALESCE(approved,0)=1")
    elif status == "pending":
        where.append("COALESCE(approved,0)=0")
    if query:
        where.append("(IFNULL(username,'') LIKE ? OR IFNULL(first_name,'') LIKE ? OR IFNULL(last_name,'') LIKE ? OR CAST(user_id AS TEXT) LIKE ?)")
        like = f"%{query}%"; params += [like, like, like, like]

    with db() as conn:
        cur = conn.execute(f"SELECT COUNT(*) FROM users WHERE {' AND '.join(where)}", tuple(params))
        total = cur.fetchone()[0]
        offset = (page-1) * PAGE_SIZE_USERS
        cur = conn.execute(
            f"""
            SELECT user_id, COALESCE(username,''), COALESCE(first_name,''), COALESCE(last_name,''),
                   COALESCE(balance_slot8,0), COALESCE(balance_slot3,0), COALESCE(balance_slot5,0), COALESCE(wallet_kwh,0),
                   COALESCE(approved,0), COALESCE(created_at,'')
            FROM users
            WHERE {' AND '.join(where)}
            ORDER BY user_id ASC
            LIMIT ? OFFSET ?
            """,
            tuple(params) + (PAGE_SIZE_USERS, offset),
        )
        rows = cur.fetchall()

    if not rows:
        await update.message.reply_text("Nessun utente trovato."); return

    lines = [f"Utenti {status} – pagina {page} di {(total+PAGE_SIZE_USERS-1)//PAGE_SIZE_USERS} – risultati: {total}"]
    for (uid, username, first_name, last_name, b8, b3, b5, wallet, appr, created_at) in rows:
        name = (first_name or "") + (" " + last_name if last_name else "")
        who = f"@{username}" if username else (name.strip() or str(uid))
        lines.append(f"• {who} (id {uid}) – Approvato: {bool(appr)} – 8:{fmt_kwh(Decimal(str(b8)))}  3:{fmt_kwh(Decimal(str(b3)))}  5:{fmt_kwh(Decimal(str(b5)))}  Wallet:{fmt_kwh(Decimal(str(wallet)))}")

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"users:page:{status}:{page-1}:{query or '-'}"))
    if offset + PAGE_SIZE_USERS < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"users:page:{status}:{page+1}:{query or '-'}"))
    buttons = [nav] if nav else []
    buttons.append([InlineKeyboardButton("🗑️ Elimina utente", callback_data="users:delete:start")])
    kb = InlineKeyboardMarkup(buttons)

    await update.message.reply_text("\n".join(lines), reply_markup=kb)

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

    if tag == "delete":
        if not is_admin(q.from_user.id):
            await q.answer("Solo admin", show_alert=True); return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Conferma", callback_data=f"userdel:yes:{uid}"),
             InlineKeyboardButton("❌ Annulla", callback_data=f"userdel:no:{uid}")]
        ])
        await q.edit_message_text(f"Confermi l'eliminazione dell'utente {uid}?", reply_markup=kb)
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
        await q.edit_message_text("Operazione annullata."); return
    with db() as conn:
        conn.execute("DELETE FROM users WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM recharges WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM wallet_requests WHERE user_id=?", (uid,))
    await q.edit_message_text(f"Utente {uid} eliminato.")

async def on_message_delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_delete_user"):
        return
    txt = (update.message.text or "").strip()
    target = resolve_user_identifier(txt)
    if target is None:
        await update.message.reply_text("Utente non trovato."); return
    if isinstance(target, list):
        await ask_user_pick(update, target, "delete"); return
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
        data = io.BytesIO(buf.getvalue().encode("utf-8")); data.name = "users.csv"
        await context.bot.send_document(chat_id=update.effective_chat.id, document=data, caption="Esportazione utenti")
        return
    if what == "recharges":
        date_from = args[1] if len(args) >= 2 else None
        date_to   = args[2] if len(args) >= 3 else None
        where = ["1=1"]; params: List[Any] = []
        if date_from:
            where.append("created_at >= ?"); params.append(date_from)
        if date_to:
            where.append("created_at <= ?||'T23:59:59'"); params.append(date_to)
        with db() as conn:
            cur = conn.execute(
                f"""
                SELECT id, user_id, slot, kwh, status, created_at, reviewed_at, reviewer_id, note, photo_file_id
                FROM recharges
                WHERE {' AND '.join(where)}
                ORDER BY id ASC
                """,
                tuple(params)
            )
            rows = cur.fetchall()
        import csv, io
        buf = io.StringIO(); w = csv.writer(buf)
        w.writerow(["id","user_id","slot","kwh","status","created_at","reviewed_at","reviewer_id","note","photo_file_id"])
        for r in rows: w.writerow(list(r))
        data = io.BytesIO(buf.getvalue().encode("utf-8")); data.name = "recharges.csv"
        await context.bot.send_document(chat_id=update.effective_chat.id, document=data, caption="Esportazione ricariche")
        return
    await update.message.reply_text("Uso: /export users | /export recharges [YYYY-MM-DD] [YYYY-MM-DD]")

# -------------------- STARTUP / PING --------------------

async def startup_notify(app: Application):
    try:
        print("[BOOT] saldo-bot avviato ✅")
        print(f"Python: {sys.version.split()[0]} • PTB: {telegram.__version__}")
        print(f"DB_PATH: {DB_PATH}")
        total_handlers = sum(len(h) for h in app.handlers.values()) if hasattr(app, "handlers") else 0
        print(f"Handlers: {total_handlers}")
        admins = list(ADMIN_IDS)
        if admins:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            for aid in admins:
                try:
                    await app.bot.send_message(chat_id=aid, text=f"🔔 saldo-bot avviato\n{ts}\nPython {sys.version.split()[0]} • PTB {telegram.__version__}")
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

# -------------------- MAIN --------------------

def main():
    if not TOKEN:
        log.error("TELEGRAM_TOKEN non impostato")
        sys.exit(1)

    migrate()

    # PTB v20: usa post_init nel builder (non .append)
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
    app.add_handler(CallbackQueryHandler(on_decl_callback, pattern=r"^decl:(start|slot:(8|3|5)|cancel)$"))

    # Messages — ORDINE IMPORTANTE: prima i flussi dichiarazione, poi resto
    app.add_handler(MessageHandler(filters.PHOTO, on_message_decl_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_decl_kwh))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_decl_note))
    # Wallet amount (utente) / wallet kWh (admin) / delete user
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_amount_wallet))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_admin_wallet_kwh))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_delete_user))

    log.info("Bot in avvio...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
