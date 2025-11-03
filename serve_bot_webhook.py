from telegram.constants import ParseMode

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await init_db()
    except Exception as e:
        log.exception("INIT_DB_FAILED: %s", e)

    user = update.effective_user
    chat = update.effective_chat
    if user:
        await ensure_user(user.id, getattr(user, "full_name", None))

    if user and (user.id in ADMIN_IDS):
        msg = (
            f"👋 *Admin* — saldo‑bot v{__VERSION__}\n\n"
            "Pannello rapido:\n"
            "• ➕ *Ricarica*: accredita kWh a un utente\n"
            "• ➖ *Addebita*: addebita kWh a un utente\n\n"
            "ℹ️ *Comandi disponibili*\n"
            "• /saldo — mostra i tuoi kWh\n"
            "• /ricarica slotX quantita\n\n"
            "👮 *Admin extra*\n"
            "• /pending — richieste in attesa\n"
            "• /approve id — approva richiesta\n"
            "• /reject id — rifiuta richiesta\n"
            "• /users — lista utenti e saldi\n"
            "• /credita chat_id slot kwh\n"
            "• /allow_negative <id> on|off|default\n"
            "• /export_ops — esporta storici\n\n"
            f"DB: `{DB_PATH}`"
        )
        kb = admin_home_kb()
    else:
        msg = (
            f"👋 Ciao {user.first_name if user else ''}! Questo è saldo‑bot v{__VERSION__}.\n\n"
            "Comandi:\n"
            "• /saldo — mostra i tuoi kWh\n"
            "• /storico — ultime operazioni\n"
            "• /ricarica slotX quantita\n"
        )
        kb = None

    try:
        await context.bot.send_message(
            chat_id=chat.id, 
            text=msg, 
            parse_mode=ParseMode.MARKDOWN_V2, 
            reply_markup=kb
        )
    except Exception as e:
        log.exception("START_REPLY_FAILED: %s", e)
        # fallback senza parse_mode per sicurezza
        await context.bot.send_message(chat_id=chat.id, text=msg, reply_markup=kb)
