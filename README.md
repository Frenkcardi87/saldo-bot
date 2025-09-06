# Saldo Bot Telegram

Bot Telegram per gestione saldi kWh con 3 wallet (Slot 8, 3, 5).  
Flusso utente: ➕ Ricarica → Slot → kWh → Foto → Nota → Conferma.  
Gestione admin: approvazione utenti, approvazione/rifiuto ricariche, accrediti manuali.

## Avvio locale

```powershell
cd saldo-bot
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot_slots_flow.py
