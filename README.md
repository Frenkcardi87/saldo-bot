# Saldo Bot - Sistema di Approvazione Ricariche v2.0.0

## 🆕 Nuove Funzionalità

### Sistema di Richiesta Ricarica
Gli utenti possono ora richiedere ricariche che devono essere approvate dagli admin prima di scalare i kWh dal saldo.

### Flusso Utente

#### Metodo 1: Comando /ricarica
1. L'utente esegue `/ricarica`
2. Seleziona lo slot da un menu inline
3. Inserisce i kWh richiesti
4. Invia una **foto obbligatoria** come prova
5. Opzionalmente aggiunge una nota
6. Conferma l'invio

#### Metodo 2: Foto con Didascalia
L'utente può inviare direttamente una foto con didascalia nel formato:
```
slot3 4.5
```
oppure con nota:
```
slot8 10 Ricarica del 3 novembre
```

### Flusso Admin

#### Notifiche
Quando arriva una nuova richiesta, tutti gli admin ricevono una notifica con:
- ID richiesta
- Dati utente (nome, username, TG ID)
- Slot e kWh richiesti
- Foto allegata
- Nota (se presente)
- Pulsanti: **✅ Approva** e **❌ Rifiuta**

#### Approvazione
Quando un admin approva:
1. I kWh vengono **scalati** dal saldo utente
2. L'operazione viene registrata nel database
3. L'utente riceve notifica con nuovo saldo
4. Il messaggio admin viene aggiornato con l'esito

#### Rifiuto
Quando un admin rifiuta:
1. La richiesta viene marcata come rifiutata
2. L'utente riceve notifica del rifiuto
3. Il messaggio admin viene aggiornato

### Comando /pending

#### Per Admin
Mostra tutte le richieste in attesa con:
- Dettagli completi di ogni richiesta
- Pulsanti per approvare/rifiutare direttamente

#### Per Utenti
Mostra solo le proprie richieste in attesa con dettagli.

### Limiti e Validazioni

- **Massimo 5 richieste pending** per utente contemporaneamente
- **Foto obbligatoria** per ogni richiesta
- Le foto vengono salvate in `/credit_photos` con nome univoco
- Validazione saldo al momento dell'approvazione
- Rispetto delle policy allow_negative esistenti

## 📁 Database

### Nuova Tabella: credit_requests

```sql
CREATE TABLE credit_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    slot TEXT NOT NULL,
    kwh REAL NOT NULL,
    photo_path TEXT,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    created_at TEXT NOT NULL,
    processed_at TEXT,
    processed_by INTEGER,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(processed_by) REFERENCES users(id)
)
```

## 🔧 Configurazione

### Variabili d'Ambiente (nuove)

```bash
# Slot disponibili (separati da virgola)
SLOTS="slot1,slot3,slot5,slot8,wallet"

# Percorso per salvare le foto delle ricariche
CREDIT_PHOTOS_PATH="/credit_photos"
```

### Variabili Esistenti
Tutte le variabili esistenti continuano a funzionare:
- `TELEGRAM_TOKEN`
- `ADMIN_IDS`
- `DB_PATH`
- `MAX_WALLET_KWH`
- `MAX_CREDIT_PER_OP`
- `ALLOW_NEGATIVE`

## 🚀 Deployment

### Railway / Servizi Cloud

1. Carica entrambi i file:
   - `bot_slots_flow.py`
   - `serve_bot_webhook.py`

2. Crea la cartella per le foto:
   ```bash
   mkdir -p /credit_photos
   ```

3. Configura le variabili d'ambiente

4. Il bot si avvierà automaticamente

### Testing Locale

```bash
python bot_slots_flow.py
```

## 📋 Comandi Disponibili

### Utenti
- `/start` - Avvia il bot
- `/saldo` - Visualizza saldo corrente
- `/ricarica` - Invia richiesta di ricarica
- `/pending` - Visualizza tue richieste in attesa
- `/storico` - Visualizza storico operazioni
- `/ping` - Health check

### Admin
Tutti i comandi utente più:
- `/pending` - Visualizza TUTTE le richieste in attesa
- `/saldo <user_id>` - Controlla saldo di un utente
- `/addebita <user_id> <kwh> [slot]` - Addebito manuale
- `/export_ops [filtri]` - Esporta operazioni in CSV
- `/allow_negative <user_id> on|off|default` - Gestisci policy saldo negativo
- `/admin` - Pannello amministrazione

## 🔄 Migrazione da Versione Precedente

Il bot gestisce automaticamente la migrazione del database:
1. Crea la nuova tabella `credit_requests` se non esiste
2. Mantiene tutti i dati esistenti
3. Aggiunge gli indici necessari

Non è richiesta alcuna azione manuale.

## 📸 Gestione Foto

Le foto vengono salvate con nome formato:
```
{user_id}_{uuid}_{timestamp}.jpg
```

Esempio:
```
123456789_a1b2c3d4_20251103_143022.jpg
```

Le foto vengono eliminate solo se l'utente annulla la richiesta prima di confermarla.

## 🔐 Sicurezza

- Solo admin possono approvare/rifiutare richieste
- Gli utenti vedono solo le proprie richieste in `/pending`
- Le foto sono salvate localmente e non accessibili via URL
- Validazione di tutti gli input utente
- Transazioni database atomiche per evitare inconsistenze

## 📊 Logging

Tutti gli eventi importanti vengono loggati:
- `CR_START` - Inizio richiesta
- `CR_SLOT_SET` - Slot selezionato
- `CR_KWH_SET` - kWh inseriti
- `CR_PHOTO_SAVED` - Foto salvata
- `CR_CREATED` - Richiesta creata
- `CREDIT_REQUEST_APPROVED` - Richiesta approvata
- `CREDIT_REQUEST_REJECTED` - Richiesta rifiutata

## 🆘 Supporto

Per problemi o domande:
1. Controlla i log del bot
2. Verifica le variabili d'ambiente
3. Assicurati che la cartella `/credit_photos` sia scrivibile
4. Verifica che gli ADMIN_IDS siano configurati correttamente

## 📝 Note Tecniche

- Bot costruito con python-telegram-bot 21.6 (async)
- Database SQLite con aiosqlite
- Supporto completo per ConversationHandler
- Gestione errori globale
- Compatibile con webhook (Railway, Heroku, ecc.) e polling

## 🔄 Changelog v2.0.0

### Aggiunte
- ✅ Sistema completo di richieste ricarica
- ✅ Upload foto obbligatorio
- ✅ Notifiche admin con pulsanti inline
- ✅ Comando /pending per admin e utenti
- ✅ Gestione approvazione con scalamento kWh
- ✅ Gestione rifiuto con notifica
- ✅ Supporto foto con didascalia
- ✅ Limite 5 richieste pending per utente
- ✅ Salvataggio foto in cartella dedicata

### Modifiche
- 🔄 Messaggio /start aggiornato con nuovi comandi
- 🔄 Slot configurabili via variabile SLOTS

### Mantenute
- ✅ Tutte le funzionalità esistenti (admin credit/debit, allow_negative, export, ecc.)
- ✅ Compatibilità backward con database esistenti
- ✅ Tutti i comandi admin esistenti
