#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Saldo Bot – nuova versione
- Libreria: python-telegram-bot 21.6 (async)
- DB: SQLite (file da env DB_PATH, altrimenti ./kwh_slots.db)

ENV richieste:
- TELEGRAM_TOKEN
- ADMIN_IDS (lista di numeri separati da virgola)
- DB_PATH (opzionale)
- ALLOW_NEGATIVE (opzionale: '1' per consentire saldi negativi)

Compatibile con deploy su Railway/Render. Può essere avviato direttamente o tramite serve_bot.py
"""

import asyncio
import csv
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =============================
# CONFIG & ENV
# =============================
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_IDS = set()
if os.getenv("ADMIN_IDS"):
    ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", os.path.abspath("kwh_slots.db"))
ALLOW_NEGATIVE = os.getenv("ALLOW_NEGATIVE", "0") == "1"

PAGE_SIZE_USERS = 10
DATE_FMT = "%Y-%m-%d %H:%M:%S"

# =============================
# DB LAYER
# =============================
import logging

def _init_db_instance() -> 'DB':
    """Try DB at DB_PATH; on failure (e.g., read-only FS on Railway), fall back to /tmp/kwh_slots.db"""
    global DB_PATH
    try:
        db = DB(DB_PATH)
        # smoke test: open connection and pragma
        with db.conn() as con:
            con.execute("PRAGMA journal_mode=WAL")
        logging.info("DB initialized at %s", DB_PATH)
        return db
    except Exception as e:
        logging.warning("DB init failed at %s: %s", DB_PATH, e)
        # fallback
        fallback = "/tmp/kwh_slots.db"
        try:
            DB_PATH = fallback
            db = DB(DB_PATH)
            with db.conn() as con:
                con.execute("PRAGMA journal_mode=WAL")
            logging.info("DB fallback initialized at %s", DB_PATH)
            return db
        except Exception as e2:
            logging.exception("DB fallback failed at /tmp: %s", e2)
            raise


class DB:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    @contextmanager
    def conn(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    def _init_db(self):
        with self.conn() as con:
            cur = con.cursor()
            # USERS
            cur.execute(
                """
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
                )
                """
            )

            # RECHARGES dichiarazioni utente (con foto)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS recharges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    slot TEXT,
                    kwh REAL,
                    photo_id TEXT,
                    note TEXT,
                    status TEXT,
                    created_at TEXT,
                    reviewed_by INTEGER,
                    reviewed_at TEXT
                )
                """
            )

            # WALLET REQUESTS
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS wallet_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    euro REAL,
                    status TEXT,
                    created_at TEXT,
                    reviewed_by INTEGER,
                    reviewed_at TEXT
                )
                """
            )

            con.commit()

    # --- USERS ---
    def ensure_user(self, tg_user) -> sqlite3.Row:
        now = datetime.now().strftime(DATE_FMT)
        with self.conn() as con:
            cur = con.cursor()
            cur.execute("SELECT * FROM users WHERE id=?", (tg_user.id,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO users (id, chat_id, username, first_name, last_name, approved,
                                       slot1_kwh, slot3_kwh, slot5_kwh, slot8_kwh, wallet_kwh,
                                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, 0,0,0,0,0, ?, ?)
                    """,
                    (
                        tg_user.id,
                        tg_user.id,
                        tg_user.username or "",
                        tg_user.first_name or "",
                        tg_user.last_name or "",
                        now,
                        now,
                    ),
                )
                con.commit()
                cur.execute("SELECT * FROM users WHERE id=?", (tg_user.id,))
                row = cur.fetchone()
            else:
                cur.execute(
                    """
                    UPDATE users
                    SET chat_id=?, username=?, first_name=?, last_name=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        tg_user.id,
                        tg_user.username or "",
                        tg_user.first_name or "",
                        tg_user.last_name or "",
                        now,
                        tg_user.id,
                    ),
                )
                con.commit()
                cur.execute("SELECT * FROM users WHERE id=?", (tg_user.id,))
                row = cur.fetchone()
        return row

    def find_users(self, state: str, term: Optional[str], page: int, page_size: int) -> Tuple[List[sqlite3.Row], int]:
        where = []
        args: List[Any] = []
        if state == "approvati":
            where.append("approved=1")
        elif state == "pending":
            where.append("approved=0")
        if term:
            where.append("(username LIKE ? OR first_name LIKE ? OR last_name LIKE ?)")
            args.extend([f"%{term}%", f"%{term}%", f"%{term}%"])
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        count_sql = f"SELECT COUNT(*) AS c FROM users{where_sql}"
        list_sql = f"SELECT * FROM users{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        with self.conn() as con:
            cur = con.cursor()
            cur.execute(count_sql, args)
            total = cur.fetchone()[0]
            cur.execute(list_sql, args + [page_size, (page-1)*page_size])
            rows = cur.fetchall()
        return rows, total

    def delete_user(self, user_id: int):
        with self.conn() as con:
            cur = con.cursor()
            cur.execute("DELETE FROM recharges WHERE user_id=?", (user_id,))
            cur.execute("DELETE FROM wallet_requests WHERE user_id=?", (user_id,))
            cur.execute("DELETE FROM users WHERE id=?", (user_id,))
            con.commit()

    # --- BALANCES ---
    def get_balances(self, user_id: int) -> sqlite3.Row:
        with self.conn() as con:
            cur = con.cursor()
            cur.execute(
                "SELECT slot1_kwh, slot3_kwh, slot5_kwh, slot8_kwh, wallet_kwh FROM users WHERE id=?",
                (user_id,),
            )
            return cur.fetchone()

    def _apply_slot(self, current: float, delta: float) -> float:
        new_val = current + delta
        if not ALLOW_NEGATIVE and new_val < 0:
            raise ValueError("Saldo negativo non consentito")
        return new_val

    def credit_slot(self, user_id: int, slot: str, kwh: float):
        col = {"1": "slot1_kwh", "3": "slot3_kwh", "5": "slot5_kwh", "8": "slot8_kwh"}.get(slot)
        if not col:
            raise ValueError("Slot non valido")
        with self.conn() as con:
            cur = con.cursor()
            cur.execute(f"SELECT {col} FROM users WHERE id=?", (user_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError("Utente non trovato")
            new_val = self._apply_slot(float(row[0]), float(kwh))
            cur.execute(f"UPDATE users SET {col}=?, updated_at=? WHERE id=?", (new_val, datetime.now().strftime(DATE_FMT), user_id))
            con.commit()

    def credit_wallet(self, user_id: int, kwh: float):
        with self.conn() as con:
            cur = con.cursor()
            cur.execute("SELECT wallet_kwh FROM users WHERE id=?", (user_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError("Utente non trovato")
            new_val = self._apply_slot(float(row[0]), float(kwh))
            cur.execute("UPDATE users SET wallet_kwh=?, updated_at=? WHERE id=?", (new_val, datetime.now().strftime(DATE_FMT), user_id))
            con.commit()

    # --- RECHARGES ---
    def insert_recharge(self, user_id: int, slot: str, kwh: float, photo_id: str, note: str) -> int:
        now = datetime.now().strftime(DATE_FMT)
        with self.conn() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO recharges (user_id, slot, kwh, photo_id, note, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (user_id, slot, kwh, photo_id, note, now),
            )
            con.commit()
            return cur.lastrowid

    def list_pending_recharges(self) -> List[sqlite3.Row]:
        with self.conn() as con:
            cur = con.cursor()
            cur.execute("SELECT * FROM recharges WHERE status='pending' ORDER BY created_at ASC")
            return cur.fetchall()

    def get_recharge(self, rid: int) -> Optional[sqlite3.Row]:
        with self.conn() as con:
            cur = con.cursor()
            cur.execute("SELECT * FROM recharges WHERE id=?", (rid,))
            return cur.fetchone()

    def set_recharge_status(self, rid: int, status: str, reviewer_id: int):
        with self.conn() as con:
            cur = con.cursor()
            cur.execute(
                "UPDATE recharges SET status=?, reviewed_by=?, reviewed_at=? WHERE id=?",
                (status, reviewer_id, datetime.now().strftime(DATE_FMT), rid),
            )
            con.commit()

    # --- WALLET REQUESTS ---
    def insert_wallet_request(self, user_id: int, euro: float) -> int:
        now = datetime.now().strftime(DATE_FMT)
        with self.conn() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO wallet_requests (user_id, euro, status, created_at) VALUES (?, ?, 'pending', ?)",
                (user_id, euro, now),
            )
            con.commit()
            return cur.lastrowid

    def list_pending_wallet(self) -> List[sqlite3.Row]:
        with self.conn() as con:
            cur = con.cursor()
            cur.execute("SELECT * FROM wallet_requests WHERE status='pending' ORDER BY created_at ASC")
            return cur.fetchall()

    def get_wallet_request(self, wid: int) -> Optional[sqlite3.Row]:
        with self.conn() as con:
            cur = con.cursor()
            cur.execute("SELECT * FROM wallet_requests WHERE id=?", (wid,))
            return cur.fetchone()

    def set_wallet_status(self, wid: int, status: str, reviewer_id: int):
        with self.conn() as con:
            cur = con.cursor()
            cur.execute(
                "UPDATE wallet_requests SET status=?, reviewed_by=?, reviewed_at=? WHERE id=?",
                (status, reviewer_id, datetime.now().strftime(DATE_FMT), wid),
            )
            con.commit()


# =============================
# UTILS & CONSTANTS
# =============================
DBI = _init_db_instance()

MENU_USER = InlineKeyboardMarkup([
    [InlineKeyboardButton("📊 Saldo", callback_data="menu:saldo")],
    [InlineKeyboardButton("📝 Dichiara ricarica", callback_data="menu:decl")],
    [InlineKeyboardButton("💳 Wallet", callback_data="menu:wallet")],
    [InlineKeyboardButton("ℹ️ Help", callback_data="menu:help")],
])

MENU_ADMIN_EXTRAS = [
    [InlineKeyboardButton("🧾 Pending", callback_data="admin:pending")],
    [InlineKeyboardButton("👛 Wallet pending", callback_data="admin:walletpending")],
    [InlineKeyboardButton("👥 Utenti", callback_data="admin:utenti")],
]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def smart_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        except Exception:
            await update.effective_chat.send_message(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    else:
        await update.effective_chat.send_message(text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)


# =============================
# COMMANDS
# =============================
async def cmd_ping(update, context):
    await update.message.reply_text("pong")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import logging
    user = update.effective_user
    try:
        DBI.ensure_user(user)
    except Exception as e:
        logging.exception("ensure_user failed: %s", e)
        await update.message.reply_text("⚠️ Errore DB in registrazione (probabile file non scrivibile). Provo comunque a mostrarti il menu.")
    buttons = [*MENU_USER.inline_keyboard]
    if is_admin(user.id):
        buttons += MENU_ADMIN_EXTRAS
    try:
        await smart_reply(update, context, "*Benvenuto!*\nSeleziona un'azione dal menu.", InlineKeyboardMarkup(buttons))
    except Exception as e:
        logging.exception("send menu failed: %s", e)
        await update.message.reply_text("⚠️ Errore nell'invio del menu. Riprova /menu")


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    buttons = [*MENU_USER.inline_keyboard]
    if is_admin(user.id):
        buttons += MENU_ADMIN_EXTRAS
    await smart_reply(update, context, "*Menu*", InlineKeyboardMarkup(buttons))

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    buttons = [*MENU_USER.inline_keyboard]
    if is_admin(user.id):
        buttons += MENU_ADMIN_EXTRAS
    help_txt = (
        "*Guida rapida*\n\n"
        "• /saldo – mostra i tuoi saldi.\n"
        "• /whoami (admin) – mostra il tuo ID.\n"
        "• /pending (admin) – ricariche in attesa.\n"
        "• /walletpending (admin) – wallet in attesa.\n"
        "• /utenti [stato] [pagina] [cerca <termine>] (admin).\n"
        "• /export users | /export recharges [YYYY-MM-DD] [YYYY-MM-DD] (admin)."
    )
    await smart_reply(update, context, help_txt, InlineKeyboardMarkup(buttons))


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await smart_reply(update, context, "Solo admin.")
    await smart_reply(update, context, f"Sei admin. ID: `{update.effective_user.id}`")


async def show_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: Optional[int] = None):
    uid = target_user_id or update.effective_user.id
    balances = DBI.get_balances(uid)
    if not balances:
        return await smart_reply(update, context, "Utente non trovato.")
    txt = (
        "*📊 Saldi*\n"
        f"Slot1: `{balances['slot1_kwh']:.2f}` kWh\n"
        f"Slot3: `{balances['slot3_kwh']:.2f}` kWh\n"
        f"Slot5: `{balances['slot5_kwh']:.2f}` kWh\n"
        f"Slot8: `{balances['slot8_kwh']:.2f}` kWh\n"
        f"Wallet: `{balances['wallet_kwh']:.2f}` kWh"
    )
    await smart_reply(update, context, txt)


async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    DBI.ensure_user(user)
    if is_admin(user.id) and context.args:
        arg = context.args[0]
        if arg.isdigit():
            return await show_saldo(update, context, int(arg))
    await show_saldo(update, context)


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await smart_reply(update, context, "Solo admin.")
    await open_pending_panel(update, context)


async def cmd_walletpending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await smart_reply(update, context, "Solo admin.")
    await open_wallet_panel(update, context)


async def cmd_utenti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await smart_reply(update, context, "Solo admin.")
    stato = "tutti"; pagina = 1; term = None
    args = context.args; i = 0
    while i < len(args):
        a = args[i]
        if a in {"tutti", "approvati", "pending"}: stato = a
        elif a.isdigit(): pagina = int(a)
        elif a == "cerca" and i+1 < len(args): term = args[i+1]; i += 1
        i += 1
    await render_users_list(update, context, stato, pagina, term)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await smart_reply(update, context, "Solo admin.")
    if not context.args:
        return await smart_reply(update, context, "Uso: /export users | /export recharges [YYYY-MM-DD] [YYYY-MM-DD]")
    kind = context.args[0]
    if kind == "users":
        path = "/mnt/data/users.csv"
        DBI.export_users(path)
        await update.effective_chat.send_document(document=open(path, "rb"), filename="users.csv")
    elif kind == "recharges":
        date_from = context.args[1] if len(context.args) > 1 else None
        date_to = context.args[2] if len(context.args) > 2 else None
        path = "/mnt/data/recharges.csv"
        DBI.export_recharges(path, date_from, date_to)
        await update.effective_chat.send_document(document=open(path, "rb"), filename="recharges.csv")
    else:
        await smart_reply(update, context, "Tipo export non valido.")


# =============================
# MENU HANDLERS
# =============================
async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    if data == "menu:saldo": return await show_saldo(update, context)
    if data == "menu:help": return await cmd_help(update, context)
    if data == "menu:decl": return await start_decl_flow(update, context)
    if data == "menu:wallet": return await start_wallet_flow(update, context)
    if data == "admin:pending":
        if not is_admin(update.effective_user.id): return await smart_reply(update, context, "Solo admin.")
        return await open_pending_panel(update, context)
    if data == "admin:walletpending":
        if not is_admin(update.effective_user.id): return await smart_reply(update, context, "Solo admin.")
        return await open_wallet_panel(update, context)
    if data == "admin:utenti":
        if not is_admin(update.effective_user.id): return await smart_reply(update, context, "Solo admin.")
        return await render_users_list(update, context, "tutti", 1, None)


# =============================
# DICHIARAZIONE RICARICA – FLOW
# =============================
async def start_decl_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    ud["decl_await_kwh"] = True; ud["decl_kwh"] = None
    ud["decl_await_photo"] = False; ud["decl_photo_id"] = None
    ud["decl_await_note"] = False; ud["decl_note"] = None
    await smart_reply(update, context, "*📝 Dichiara ricarica*\nInserisci i kWh (numero > 0):")


def _parse_positive_decimal(text: str) -> Optional[Decimal]:
    try:
        d = Decimal(text.replace(",", ".").strip())
        if d > 0: return d
    except InvalidOperation:
        pass
    return None


async def on_message_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    text = update.message.text.strip()

    # Wallet kWh attesi da admin
    if is_admin(update.effective_user.id) and ud.get("awaiting_wallet_kwh_for"):
        wid = ud.get("awaiting_wallet_kwh_for")
        amount = _parse_positive_decimal(text)
        if not amount: return await update.message.reply_text("Inserisci un numero di kWh valido (>0).")
        req = DBI.get_wallet_request(wid)
        if not req:
            ud["awaiting_wallet_kwh_for"] = None
            return await update.message.reply_text("Richiesta non trovata.")
        DBI.credit_wallet(req["user_id"], float(amount))
        DBI.set_wallet_status(wid, "approved", update.effective_user.id)
        ud["awaiting_wallet_kwh_for"] = None
        await update.message.reply_text(f"Wallet approvato ✅ (+{amount} kWh)")
        await context.bot.send_message(chat_id=req["user_id"], text=f"La tua richiesta wallet #{wid} è stata *approvata*: +{amount} kWh", parse_mode=ParseMode.MARKDOWN)
        return

    # Delete flow admin
    if is_admin(update.effective_user.id) and ud.get("awaiting_delete_user"):
        val = re.sub(r"[^0-9]", "", text)
        if not val: return await update.message.reply_text("Invia un ID numerico valido dell'utente da eliminare.")
        target_id = int(val)
        ud["awaiting_delete_user"] = False
        ud["awaiting_delete_user_confirm"] = target_id
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Sì, elimina", callback_data=f"userdel:yes:{target_id}")],
                                   [InlineKeyboardButton("❌ No", callback_data=f"userdel:no:{target_id}")]])
        return await update.message.reply_text(f"Confermi eliminazione utente `{target_id}`?", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    # Step kWh
    if ud.get("decl_await_kwh"):
        amount = _parse_positive_decimal(text)
        if not amount: return await update.message.reply_text("Per favore inserisci un *numero* kWh valido (>0).", parse_mode=ParseMode.MARKDOWN)
        ud["decl_kwh"] = str(amount); ud["decl_await_kwh"] = False; ud["decl_await_photo"] = True
        return await update.message.reply_text("Ora invia *la foto della ricevuta* (obbligatoria).", parse_mode=ParseMode.MARKDOWN)

    # Nota opzionale
    if ud.get("decl_await_note"):
        ud["decl_note"] = text[:500]; ud["decl_await_note"] = False
        return await ask_slot_choice(update, context)

    await update.message.reply_text("Comando non riconosciuto. Usa /help o il menu.")


async def on_message_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    if ud.get("decl_await_photo"):
        if not update.message.photo:
            return await update.message.reply_text("Nessuna foto rilevata. Invia una *foto*.", parse_mode=ParseMode.MARKDOWN)
        photo = update.message.photo[-1]
        ud["decl_photo_id"] = photo.file_id; ud["decl_await_photo"] = False
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Aggiungi nota", callback_data="decl:note:add")],
                                   [InlineKeyboardButton("➡️ Procedi senza nota", callback_data="decl:note:skip")]])
        return await update.message.reply_text("Foto ricevuta. Vuoi aggiungere una nota?", reply_markup=kb)
    await update.message.reply_text("Foto non attesa in questo momento.")


async def on_decl_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data; ud = context.user_data

    if data == "decl:note:add":
        ud["decl_await_note"] = True
        return await smart_reply(update, context, "Scrivi la *nota* (max 500 caratteri):")

    if data == "decl:note:skip":
        ud["decl_note"] = ""
        return await ask_slot_choice(update, context)

    if data.startswith("decl:slot:"):
        slot = data.split(":")[-1]
        if not ud.get("decl_kwh"): return await smart_reply(update, context, "Manca il valore *kWh*. Reinvia i kWh.")
        if not ud.get("decl_photo_id"): return await smart_reply(update, context, "Manca la *foto*. Invia la foto prima di scegliere lo slot.")
        kwh = float(str(ud.get("decl_kwh"))); note = ud.get("decl_note") or ""
        rid = DBI.insert_recharge(update.effective_user.id, slot, kwh, ud.get("decl_photo_id"), note)
        # clean
        for k in ["decl_await_kwh","decl_kwh","decl_await_photo","decl_photo_id","decl_await_note","decl_note"]:
            ud[k] = None if "await" not in k else False
        await smart_reply(update, context, f"Ricarica inviata ✅ (id `#{rid}`) – Slot *{slot}*, {kwh} kWh.")

        text = (f"🆕 *Dichiarazione ricarica* #{rid}\n"
                f"Utente: `{update.effective_user.id}`\n"
                f"Slot: *{slot}*\n"
                f"kWh: *{kwh}*\n"
                f"Nota: {note if note else '-'}\n"
                f"Data: {datetime.now().strftime(DATE_FMT)}")
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=aid, text=text, parse_mode=ParseMode.MARKDOWN)
                rec = DBI.get_recharge(rid)
                if rec and rec["photo_id"]:
                    await context.bot.send_photo(chat_id=aid, photo=rec["photo_id"], caption=f"Foto ricarica #{rid}")
            except Exception:
                pass
        return


async def ask_slot_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Slot 8", callback_data="decl:slot:8")],
        [InlineKeyboardButton("Slot 3", callback_data="decl:slot:3")],
        [InlineKeyboardButton("Slot 5", callback_data="decl:slot:5")],
        [InlineKeyboardButton("Slot 1", callback_data="decl:slot:1")],
    ])
    return await smart_reply(update, context, "Scegli lo *slot*:", kb)


# =============================
# WALLET FLOW (utente)
# =============================
async def start_wallet_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["wallet_await_euro"] = True
    return await smart_reply(update, context, "*💳 Wallet*\nInserisci l'importo in € (numero > 0):")


async def on_wallet_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    if ud.get("wallet_await_euro"):
        amount = _parse_positive_decimal(update.message.text)
        if not amount: return await update.message.reply_text("Inserisci un importo valido (>0).")
        ud["wallet_await_euro"] = False
        rid = DBI.insert_wallet_request(update.effective_user.id, float(amount))
        await update.message.reply_text(f"Richiesta inviata ✅ (id `#{rid}`) – Importo: €{amount}", parse_mode=ParseMode.MARKDOWN)
        text = (f"🆕 *Richiesta wallet* #{rid}\n"
                f"Utente: `{update.effective_user.id}`\n"
                f"Importo: *€{amount}*\n"
                f"Data: {datetime.now().strftime(DATE_FMT)}")
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=aid, text=text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass


# =============================
# PENDING RECHARGES – ADMIN PANEL
# =============================
async def open_pending_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = DBI.list_pending_recharges()
    if not pending: return await smart_reply(update, context, "Nessuna ricarica in attesa.")
    context.user_data["pending_ids"] = [p["id"] for p in pending]
    context.user_data["pending_idx"] = 0
    await render_pending_card(update, context)


async def render_pending_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ids = context.user_data.get("pending_ids", [])
    idx = context.user_data.get("pending_idx", 0)
    if idx < 0 or idx >= len(ids): return await smart_reply(update, context, "Indice fuori lista.")
    rid = ids[idx]; rec = DBI.get_recharge(rid)
    if not rec: return await smart_reply(update, context, "Record non trovato.")

    txt = (f"*🧾 Ricarica pending* #{rec['id']}\n"
           f"Utente: `{rec['user_id']}`\n"
           f"Slot: *{rec['slot']}*\n"
           f"kWh: *{rec['kwh']}*\n"
           f"Nota: {rec['note'] if rec['note'] else '-'}\n"
           f"Stato: *{rec['status']}*\n"
           f"Data: {rec['created_at']}")

    nav = [InlineKeyboardButton("⬅ Prev", callback_data="pend:prev"),
           InlineKeyboardButton("➡ Next", callback_data="pend:next")]
    row2 = [InlineKeyboardButton("📸 Foto", callback_data=f"pend:photo:{rid}"),
            InlineKeyboardButton("ℹ Info", callback_data=f"pend:info:{rid}")]
    row3 = [InlineKeyboardButton("✅ Approva", callback_data=f"pend:approve:{rid}"),
            InlineKeyboardButton("❌ Rifiuta", callback_data=f"pend:reject:{rid}")]
    kb = InlineKeyboardMarkup([nav, row2, row3])
    await smart_reply(update, context, txt, kb)


async def on_pending_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data
    if data == "pend:prev":
        context.user_data["pending_idx"] = max(0, context.user_data.get("pending_idx", 0) - 1)
        return await render_pending_card(update, context)
    if data == "pend:next":
        context.user_data["pending_idx"] = context.user_data.get("pending_idx", 0) + 1
        return await render_pending_card(update, context)
    if data.startswith("pend:photo:"):
        rid = int(data.split(":")[-1]); rec = DBI.get_recharge(rid)
        if rec and rec["photo_id"]: await update.effective_chat.send_photo(photo=rec["photo_id"], caption=f"Foto ricarica #{rid}")
        else: await smart_reply(update, context, "Nessuna foto.")
        return
    if data.startswith("pend:info:"):
        rid = int(data.split(":")[-1]); rec = DBI.get_recharge(rid)
        if not rec: return await smart_reply(update, context, "Record non trovato.")
        return await smart_reply(update, context, f"Riepilogo ricarica #{rid}: utente `{rec['user_id']}`, slot {rec['slot']}, kWh {rec['kwh']}, stato {rec['status']}.")
    if data.startswith("pend:approve:"):
        rid = int(data.split(":")[-1]); rec = DBI.get_recharge(rid)
        if not rec: return await smart_reply(update, context, "Record non trovato.")
        try:
            DBI.credit_slot(rec["user_id"], rec["slot"], float(rec["kwh"]))
            DBI.set_recharge_status(rid, "approved", update.effective_user.id)
            await smart_reply(update, context, f"Ricarica #{rid} *approvata* ✅")
            try: await context.bot.send_message(chat_id=rec["user_id"], text=f"La tua ricarica #{rid} è stata *approvata* ✅", parse_mode=ParseMode.MARKDOWN)
            except Exception: pass
        except Exception as e:
            await smart_reply(update, context, f"Errore accredito: {e}")
        return
    if data.startswith("pend:reject:"):
        rid = int(data.split(":")[-1]); rec = DBI.get_recharge(rid)
        if not rec: return await smart_reply(update, context, "Record non trovato.")
        DBI.set_recharge_status(rid, "rejected", update.effective_user.id)
        await smart_reply(update, context, f"Ricarica #{rid} *rifiutata* ❌")
        try: await context.bot.send_message(chat_id=rec["user_id"], text=f"La tua ricarica #{rid} è stata *rifiutata* ❌", parse_mode=ParseMode.MARKDOWN)
        except Exception: pass
        return


# =============================
# WALLET PENDING – ADMIN PANEL
# =============================
async def open_wallet_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = DBI.list_pending_wallet()
    if not pending: return await smart_reply(update, context, "Nessun wallet in attesa.")
    context.user_data["wallet_ids"] = [p["id"] for p in pending]
    context.user_data["wallet_idx"] = 0
    await render_wallet_card(update, context)


async def render_wallet_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ids = context.user_data.get("wallet_ids", []); idx = context.user_data.get("wallet_idx", 0)
    if idx < 0 or idx >= len(ids): return await smart_reply(update, context, "Indice fuori lista.")
    wid = ids[idx]; rec = DBI.get_wallet_request(wid)
    if not rec: return await smart_reply(update, context, "Record non trovato.")

    txt = (f"*👛 Wallet pending* #{rec['id']}\n"
           f"Utente: `{rec['user_id']}`\n"
           f"Importo: *€{rec['euro']}*\n"
           f"Stato: *{rec['status']}*\n"
           f"Data: {rec['created_at']}")

    nav = [InlineKeyboardButton("⬅ Prev", callback_data="wal:prev"),
           InlineKeyboardButton("➡ Next", callback_data="wal:next")]
    row2 = [InlineKeyboardButton("✅ Accetta", callback_data=f"wal:accept:{wid}"),
            InlineKeyboardButton("❌ Rifiuta", callback_data=f"wal:reject:{wid}")]
    kb = InlineKeyboardMarkup([nav, row2])
    await smart_reply(update, context, txt, kb)


async def on_wallet_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data
    if data == "wal:prev":
        context.user_data["wallet_idx"] = max(0, context.user_data.get("wallet_idx", 0) - 1)
        return await render_wallet_card(update, context)
    if data == "wal:next":
        context.user_data["wallet_idx"] = context.user_data.get("wallet_idx", 0) + 1
        return await render_wallet_card(update, context)
    if data.startswith("wal:accept:"):
        wid = int(data.split(":")[-1])
        context.user_data["awaiting_wallet_kwh_for"] = wid
        return await smart_reply(update, context, f"Digita i *kWh* da accreditare per wallet #{wid}:")
    if data.startswith("wal:reject:"):
        wid = int(data.split(":")[-1])
        DBI.set_wallet_status(wid, "rejected", update.effective_user.id)
        await smart_reply(update, context, f"Wallet #{wid} *rifiutato* ❌")
        rec = DBI.get_wallet_request(wid)
        if rec:
            try: await context.bot.send_message(chat_id=rec["user_id"], text=f"La tua richiesta wallet #{wid} è stata *rifiutata* ❌", parse_mode=ParseMode.MARKDOWN)
            except Exception: pass
        return


# =============================
# UTENTI – LISTA & DELETE FLOW
# =============================
async def render_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE, stato: str, pagina: int, term: Optional[str]):
    rows, total = DBI.find_users(stato, term, pagina, PAGE_SIZE_USERS)
    if not rows: return await smart_reply(update, context, "Nessun utente trovato.")

    lines = [f"*👥 Utenti* – stato: `{stato}` – pagina {pagina}\nTotale risultati: {total}\n"]
    for r in rows:
        lines.append(
            (f"• `{r['id']}` – @{r['username'] or '-'} – {r['first_name'] or ''} {r['last_name'] or ''}\n"
             f"  slot1 {r['slot1_kwh']:.2f} | slot3 {r['slot3_kwh']:.2f} | slot5 {r['slot5_kwh']:.2f} | slot8 {r['slot8_kwh']:.2f} | wallet {r['wallet_kwh']:.2f}\n"
             f"  approvato: {bool(r['approved'])}")
        )
    prev_btn = InlineKeyboardButton("Prev", callback_data=f"users:nav:{stato}:{max(1,pagina-1)}:{term or ''}")
    next_btn = InlineKeyboardButton("Next", callback_data=f"users:nav:{stato}:{pagina+1}:{term or ''}")
    del_btn  = InlineKeyboardButton("🗑️ Elimina utente", callback_data="users:delete:start")
    kb = InlineKeyboardMarkup([[prev_btn, next_btn],[del_btn]])
    await smart_reply(update, context, "\n".join(lines), kb)


async def on_users_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; data = q.data
    if data.startswith("users:nav:"):
        _,_, stato, pagina, term = data.split(":", 4)
        return await render_users_list(update, context, stato, int(pagina), term if term else None)
    if data == "users:delete:start":
        context.user_data["awaiting_delete_user"] = True
        return await smart_reply(update, context, "Invia *ID utente* (numero) da eliminare:")
    if data.startswith("userdel:"):
        _, choice, sid = data.split(":"); uid = int(sid)
        if choice == "yes":
            DBI.delete_user(uid); await smart_reply(update, context, f"Utente `{uid}` eliminato ✅")
        else:
            await smart_reply(update, context, "Eliminazione annullata.")
        context.user_data["awaiting_delete_user_confirm"] = None
        return


# =============================
# LIFECYCLE & MAIN
# =============================
async def _notify_admins_started(app: Application):
    text = "✅ Bot avviato"
    for aid in ADMIN_IDS:
        try:
            await app.bot.send_message(chat_id=aid, text=text)
        except Exception:
            pass


async def on_error(update, context):
    logging.exception("Exception while handling an update: %s", context.error)


def build_application() -> Application:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN mancante")
    builder = ApplicationBuilder().token(TOKEN).concurrent_updates(True)
    # attach post-init notifier here (PTB v21.x)
    builder.post_init(_notify_admins_started)
    app = builder.build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("walletpending", cmd_walletpending))
    app.add_handler(CommandHandler("utenti", cmd_utenti))
    app.add_handler(CommandHandler("export", cmd_export))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^(menu:|admin:).+"))
    app.add_handler(CallbackQueryHandler(on_decl_callbacks, pattern=r"^(decl:).+"))
    app.add_handler(CallbackQueryHandler(on_pending_callbacks, pattern=r"^(pend:).+"))
    app.add_handler(CallbackQueryHandler(on_wallet_callbacks, pattern=r"^(wal:).+"))
    app.add_handler(CallbackQueryHandler(on_users_callbacks, pattern=r"^(users:|userdel:).+"))

    # Messages
    app.add_handler(MessageHandler(filters.PHOTO, on_message_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_wallet_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message_text))
    return app


def main():
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(name)s: %(message)s')
    app = build_application()
    app.add_error_handler(on_error)
    from telegram import Update
    app.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
