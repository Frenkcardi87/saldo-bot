import os
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN, InvalidOperation

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
KWH_PER_EUR = Decimal(os.getenv("KWH_PER_EUR", "1.0"))

DB_PATH = "kwh_bot.db"

# ----------------- Utility Decimali -----------------
Q = Decimal("0.0001")  # precisione a 4 decimali

def q4(x: Decimal) -> Decimal:
    """Quantizza a 4 decimali, arrotondando per difetto."""
    return x.quantize(Q, rounding=ROUND_DOWN)

def parse_kwh(arg: str) -> Decimal:
    """Parse robusto (punto o virgola) e max 4 decimali (>0)."""
    try:
        s = arg.strip().replace(",", ".")
        d = Decimal(s)
        d = q4(d)
        if d <= 0:
            raise ValueError("kWh deve essere > 0")
        return d
    except (InvalidOperation, ValueError):
        raise

def parse_amount_any(arg: str) -> Decimal:
    """Parsing generale per accrediti, consente anche valori negativi."""
    s = arg.strip().replace(",", ".")
    d = Decimal(s)
    return q4(d)

def fmt_kwh(d: Decimal) -> str:
    """Formattazione senza zeri inutili."""
    s = f"{q4(d):f}"  # toglie trailing zeros automaticamente
    if "." in s:
        intp, decp = s.split(".", 1)
        decp = decp[:4]
        decp = decp.rstrip("0")
        return intp if decp == "" else f"{intp}.{decp}"
    return s

# ----------------- DB -----------------
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
            balance_kwh REAL DEFAULT 0
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS recharges(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
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

def ensure_user(u):
    with db() as conn:
        conn.execute("""
        INSERT OR IGNORE INTO users(user_id, username, first_name, last_name)
        VALUES(?,?,?,?)
        """, (u.id, u.username or "", u.first_name or "", u.last_name or ""))
        conn.execute("""
        UPDATE users SET username=?, first_name=?, last_name=? WHERE user_id=?
        """, (u.username or "", u.first_name or "", u.last_name or "", u.id))

def get_balance(user_id) -> Decimal:
    with db() as conn:
        cur = conn.execute("SELECT balance_kwh FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row or row[0] is None:
            return q4(Decimal("0"))
        return q4(Decimal(str(row[0])))

def set_balance(user_id, new_bal: Decimal):
    with db() as conn:
        conn.execute("UPDATE users SET balance_kwh=? WHERE user_id=?",
                     (float(q4(new_bal)), user_id))

def add_balance(user_id, delta: Decimal):
    bal = get_balance(user_id)
    set_balance(user_id, bal + q4(delta))

def create_recharge(user_id, kwh: Decimal, photo_file_id) -> int:
    with db() as conn:
        cur = conn.execute("""
        INSERT INTO recharges(user_id, kwh, photo_file_id, status, created_at)
        VALUES(?, ?, ?, 'pending', ?)
        """, (user_id, float(q4(kwh)), photo_file_id, datetime.utcnow().isoformat()))
        return cur.lastrowid

def get_recharge(rid):
    with db() as conn:
        cur = conn.execute("SELECT id,user_id,kwh,photo_file_id,status FROM recharges WHERE id=?", (rid,))
        return cur.fetchone()

def set_recharge_status(rid, status, reviewer_id, note=None):
    with db() as conn:
        conn.execute("""
        UPDATE recharges
        SET status=?, reviewed_at=?, reviewer_id=?, note=?
        WHERE id=?
        """, (status, datetime.utcnow().isoformat(), reviewer_id, note, rid))

# ----------------- Helpers -----------------
def is_admin(user_id:int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID

# ----------------- Handlers Utente -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    await update.message.reply_text(
        "Ciao dolcezza! 🌟 Sei registratə.\n\n"
        "Comandi:\n"
        "• /saldo – saldo in kWh\n"
        "• /ricarica <kwh> – dichiara ricarica, poi invia la foto\n"
        "Esempio: /ricarica 12.3456"
    )

async def saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)
    bal = get_balance(user.id)
    await update.message.reply_text(f"🔋 Saldo attuale: {fmt_kwh(bal)} kWh")

# Step 1: /ricarica <kwh>
async def ricarica(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    if len(context.args) != 1:
        await update.message.reply_text("Usa: /ricarica <kwh>\nEs: /ricarica 12.3456")
        return

    try:
        kwh = parse_kwh(context.args[0])  # >0 e max 4 decimali
    except Exception:
        await update.message.reply_text("Valore kWh non valido. Esempio: /ricarica 12.3456")
        return

    current = get_balance(user.id)
    after = current - kwh
    context.user_data["pending_kwh"] = str(kwh)  # salvo come string per sicurezza

    await update.message.reply_text(
        f"Ok 💚 Hai dichiarato: {fmt_kwh(kwh)} kWh.\n"
        f"Anteprima: ora {fmt_kwh(current)} → dopo {fmt_kwh(after)} kWh.\n"
        "📷 Ora inviami la *foto della ricarica* per completare la richiesta.",
        parse_mode="Markdown"
    )

# Step 2: ricezione foto -> crea richiesta e notifica admin
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    pending_s = context.user_data.get("pending_kwh")
    if not pending_s:
        await update.message.reply_text("Per favore prima usa /ricarica <kwh>, poi invia la foto 😊")
        return

    try:
        pending_kwh = q4(Decimal(pending_s))
    except Exception:
        await update.message.reply_text("C'è stato un problema con il valore kWh. Ripeti /ricarica.")
        context.user_data.pop("pending_kwh", None)
        return

    # migliore qualità
    photo = update.message.photo[-1]
    file_id = photo.file_id

    rid = create_recharge(user.id, pending_kwh, file_id)
    context.user_data.pop("pending_kwh", None)

    # Notifica Admin
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approva", callback_data=f"approve:{rid}"),
         InlineKeyboardButton("❌ Rifiuta",  callback_data=f"reject:{rid}")]
    ])

    bal = get_balance(user.id)
    after = bal - pending_kwh

    caption = (
        f"📨 *Nuova richiesta ricarica*\n"
        f"ID: #{rid}\n"
        f"Utente: {user.first_name or ''} @{user.username or ''} (id {user.id})\n"
        f"Richiesta: {fmt_kwh(pending_kwh)} kWh\n"
        f"Saldo attuale: {fmt_kwh(bal)} kWh → dopo: {fmt_kwh(after)} kWh\n"
        f"Foto allegata qui sotto."
    )

    try:
        if ADMIN_ID:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=file_id,
                caption=caption,
                reply_markup=kb,
                parse_mode="Markdown"
            )
    except Exception:
        await update.message.reply_text("Richiesta registrata, ma non sono riuscito ad avvisare l'admin.")
    else:
        await update.message.reply_text(
            f"Perfetto! Ho inviato la tua richiesta di {fmt_kwh(pending_kwh)} kWh all'admin. "
            "Riceverai un messaggio quando verrà approvata o rifiutata. 💌"
        )

# ----------------- Callback Admin: Approva/Rifiuta -----------------
async def on_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if not is_admin(user.id):
        await query.answer("⛔ Solo l'admin può gestire questa richiesta.", show_alert=True)
        return

    try:
        action, rid_str = query.data.split(":")
        rid = int(rid_str)
    except Exception:
        await query.edit_message_caption(caption="Errore: callback non valida.")
        return

    rec = get_recharge(rid)
    if not rec:
        await query.edit_message_caption(caption="Questa richiesta non esiste più.")
        return

    _id, u_id, kwh_f, file_id, status = rec
    if status != "pending":
        await query.edit_message_caption(caption=f"Richiesta #{rid} già {status}.")
        return

    kwh = q4(Decimal(str(kwh_f)))

    if action == "approve":
        # Scala saldo anche se va in negativo
        add_balance(u_id, -kwh)
        set_recharge_status(rid, "approved", user.id)

        new_bal = get_balance(u_id)
        try:
            await context.bot.send_message(
                chat_id=u_id,
                text=f"✅ Ricarica #{rid} APPROVATA!\n"
                     f"- {fmt_kwh(kwh)} kWh scalati.\n"
                     f"🔋 Nuovo saldo: {fmt_kwh(new_bal)} kWh."
            )
        except:
            pass

        await query.edit_message_caption(
            caption=(f"✅ Richiesta #{rid} APPROVATA.\n"
                     f"Utente {u_id} −{fmt_kwh(kwh)} kWh → saldo {fmt_kwh(new_bal)} kWh.")
        )

    elif action == "reject":
        set_recharge_status(rid, "rejected", user.id)
        try:
            await context.bot.send_message(
                chat_id=u_id,
                text=f"❌ La tua ricarica #{rid} è stata rifiutata dall'admin."
            )
        except:
            pass
        await query.edit_message_caption(caption=f"❌ Richiesta #{rid} RIFIUTATA.")

# ----------------- Comandi Admin -----------------
async def credita(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Uso: /credita <user_id> <kwh>")
        return
    try:
        uid = int(context.args[0])
        kwh = parse_amount_any(context.args[1])
        add_balance(uid, kwh)
        new_bal = get_balance(uid)
        await update.message.reply_text(
            f"✅ Variazione {fmt_kwh(kwh)} kWh all'utente {uid}. Nuovo saldo: {fmt_kwh(new_bal)} kWh."
        )
        try:
            sign = "+" if kwh >= 0 else ""
            await context.bot.send_message(
                chat_id=uid,
                text=f"💳 Saldo aggiornato: {sign}{fmt_kwh(kwh)} kWh.\n"
                     f"🔋 Nuovo saldo: {fmt_kwh(new_bal)} kWh."
            )
        except:
            pass
    except Exception as e:
        await update.message.reply_text(f"Errore: {e}")

async def creditaeur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Uso: /creditaeur <user_id> <euro>")
        return
    try:
        uid = int(context.args[0])
        eur = parse_amount_any(context.args[1])
        kwh = q4(eur * KWH_PER_EUR)
        add_balance(uid, kwh)
        new_bal = get_balance(uid)
        await update.message.reply_text(
            f"✅ Accredito {fmt_kwh(eur)} € ⇒ {fmt_kwh(kwh)} kWh all'utente {uid}. Saldo: {fmt_kwh(new_bal)} kWh."
        )
        try:
            sign = "+" if kwh >= 0 else ""
            await context.bot.send_message(
                chat_id=uid,
                text=f"💳 Accredito {fmt_kwh(eur)} € ({sign}{fmt_kwh(kwh)} kWh).\n"
                     f"🔋 Nuovo saldo: {fmt_kwh(new_bal)} kWh."
            )
        except:
            pass
    except Exception as e:
        await update.message.reply_text(f"Errore: {e}")

async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with db() as conn:
        rows = conn.execute("""
            SELECT user_id, username, first_name, last_name, balance_kwh
            FROM users ORDER BY balance_kwh DESC
        """).fetchall()
    if not rows:
        await update.message.reply_text("Nessun utente registrato.")
        return
    lines = ["👥 *Utenti* (saldo kWh):"]
    for uid, uname, fn, ln, bal in rows:
        tag = f"@{uname}" if uname else f"{(fn or '').strip()} {(ln or '').strip()}".strip()
        lines.append(f"- {tag} (id {uid}): {fmt_kwh(Decimal(str(bal or 0)))} kWh")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ----------------- Fallback Testo -----------------
async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        ensure_user(update.effective_user)
    await update.message.reply_text(
        "Ciao 💙\n• /saldo per vedere il saldo\n• /ricarica <kwh> (max 4 decimali)\n"
        "Poi inviami la *foto* della ricarica per mandarla all’admin.",
        parse_mode="Markdown"
    )

# ----------------- Main -----------------
def main():
    init_db()
    if not TOKEN or ADMIN_ID == 0:
        raise RuntimeError("Imposta TELEGRAM_TOKEN e ADMIN_ID nelle variabili d'ambiente.")

    app = Application.builder().token(TOKEN).build()

    # Utente
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saldo", saldo))
    app.add_handler(CommandHandler("ricarica", ricarica))

    # Admin
    app.add_handler(CommandHandler("credita", credita))
    app.add_handler(CommandHandler("creditaeur", creditaeur))
    app.add_handler(CommandHandler("users", users_list))

    # Foto + callback review
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(CallbackQueryHandler(on_review, pattern=r"^(approve|reject):\d+$"))

    # Fallback testo
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    app.run_polling()

if __name__ == "__main__":
    main()
