saldo-bot (Telegram)

Bot Telegram (PTB v20, async) con SQLite per gestione saldi kWh, richieste ricarica wallet, approvazioni admin, paginazione liste, export CSV e notifiche.

✨ Funzionalità

/start, /help, /whoami

/saldo (utente): mostra Slot 8/3/5 + Wallet kWh

/saldo <utente> (admin): cerca per ID, @username o nome (con selezione se ambigua)

Tastiere:

principale: “💳 Wallet • X kWh” dinamico

slot: “Slot 8/3/5 • kWh”

Ricariche utenti: /pending (paginazione) con foto/dettagli/approva/rifiuta

Wallet top-up:

utente → “💳 Ricarica wallet” → inserisce €

admin → /walletpending → accetta con kWh o rifiuta → utente notificato

Lista utenti: /utenti (paginazione, filtro, ricerca) + elimina utente con conferma

Export CSV: /export users | recharges [from] [to]

Log di avvio + notifica agli admin al boot

Ping giornaliero agli admin ogni 24h

📦 Requisiti

Python 3.11 consigliato (funziona anche 3.10)

python-telegram-bot >= 20.6

SQLite (incluso in Python)

requirements.txt:

python-telegram-bot>=20.6

🚀 Avvio locale (sviluppo)

Crea un bot su @BotFather e prendi il token

Esporta le variabili (bash):

export TELEGRAM_TOKEN=123:ABC
export ADMIN_IDS=111111,222222
# opzionale:
# export DB_PATH=./kwh_slots.db
# export ALLOW_NEGATIVE=0   # per impedire sconfinamenti oltre saldo


Avvia:

python -u bot_slots_flow.py


All’avvio vedrai nei log:

[BOOT] saldo-bot avviato ✅
Python: X.Y • PTB: Z.Z
DB_PATH: ...
Handlers: N


e gli admin riceveranno un messaggio “🔔 saldo-bot avviato”.

☁️ Deploy su Railway

Consigliato: Procfile + Worker.

Procfile (root repo):

worker: python -u bot_slots_flow.py


Service type: Worker (non Web)

Start Command (se non usi Procfile): python -u bot_slots_flow.py

Environment Variables:

TELEGRAM_TOKEN = 123:ABC

ADMIN_IDS = 111111,222222

(dopo aver creato il volume) DB_PATH=/data/kwh_slots.db

(opzionale) ALLOW_NEGATIVE=0

Volume persistente:

Railway → Service → Volumes → Add Volume, mount path: /data

Imposta DB_PATH=/data/kwh_slots.db

Nota: prima rimettere in piedi il bot, poi spostare il DB su volume. Vedi sezione “📁 Spostare il DB”.

🔐 Variabili d’ambiente

TELEGRAM_TOKEN (obbligatoria) — token del bot

ADMIN_IDS (obbligatoria) — lista di ID admin separati da virgola

DB_PATH — path del file SQLite (default kwh_slots.db, in prod: /data/kwh_slots.db)

ALLOW_NEGATIVE — 1/true consente di andare sotto zero (default: abilitato); 0/false per bloccare sconfinamenti

🧭 Comandi principali

Utente

/start — attiva il bot

/saldo — mostra saldi (slot + wallet)

💳 Ricarica wallet — invia richiesta con importo in €

Admin

/saldo <utente> — ID / @username / nome (match parziale; se più risultati → bottoni)

/pending — ricariche utenti in attesa (paginazione, foto/info/approva/rifiuta)

/walletpending — richieste wallet in attesa (paginazione, accetta con kWh / rifiuta)

/utenti [tutti|approvati|pending] [pagina] [cerca <termine>]

/export users — CSV utenti

/export recharges [YYYY-MM-DD] [YYYY-MM-DD] — CSV ricariche filtrate

🧹 Pulizia repo consigliata

Tieni:

bot_slots_flow.py
requirements.txt
Procfile
README.md
.gitignore


Evita di tenere in repo:

Database (*.db) → usa un Volume (Railway)

Copie/backup di script vecchi

.env (usa le env vars del servizio)

.gitignore consigliato:

__pycache__/
*.py[cod]
*.log
.env
.venv/
venv/
*.db
.DS_Store
.idea/
.vscode/

📁 Spostare il DB (dopo che il bot funziona)

Crea un Volume su Railway (mount: /data)

Imposta DB_PATH=/data/kwh_slots.db

Avvio da zero: il bot crea tabelle al boot
Oppure migra i dati dal vecchio DB:

locale → sqlite3 kwh_slots.db ".dump" > dump.sql

railway shell → crea /data, carica dump.sql e:

sqlite3 /data/kwh_slots.db < /data/dump.sql


riavvia il servizio

🛠️ Troubleshooting

SyntaxError / ImportError: verifica Python 3.10+ e python-telegram-bot>=20.6

Bot non risponde: controlla TELEGRAM_TOKEN; verifica che il servizio sia Worker e non Web

DB in sola lettura: assicurati che DB_PATH punti a un percorso scrivibile (in Railway: /data)

Notifiche admin: verifica ADMIN_IDS corretti

Ping giornaliero duplicato**:** non avviare più istanze del worker

📄 Licenza

Progetto interno. Tutti i diritti riservati (o inserisci la tua licenza).
