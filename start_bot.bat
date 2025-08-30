@echo off
setlocal enableextensions
title Avvio Bot Telegram - saldo-bot
REM Vai nella cartella dove si trova questo .bat
pushd "%~dp0"

echo [1/6] Verifica Python...
where py >nul 2>nul
if errorlevel 1 (
  where python >nul 2>nul || (
    echo ERRORE: Python non trovato. Installa Python (64-bit) da https://www.python.org/downloads/windows/
    pause
    exit /b 1
  )
)

echo [2/6] Crea virtualenv se mancante...
if not exist ".venv\Scripts\python.exe" (
  echo Creazione venv...
  py -m venv .venv 2>nul || python -m venv .venv
)

echo [3/6] Attivo virtualenv...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
  echo ERRORE: impossibile attivare la venv.
  pause
  exit /b 1
)

echo [4/6] Imposto variabili d'ambiente...
REM ======== COMPILA QUI I TUOI VALORI =========
REM TOKEN del bot (da @BotFather)
set "8424568716:AAEGKUA8ZKQhyJvFrFLWIQKgjYegwCZVymc"


REM (B) Multi-admin (separa con virgola):
set "ADMIN_IDS=292266556,7725554135,29888809"
REM =============================================

if "%TELEGRAM_TOKEN%"=="8424568716:AAEGKUA8ZKQhyJvFrFLWIQKgjYegwCZVymc" (
  echo ERRORE: Devi impostare TELEGRAM_TOKEN dentro start_bot.bat
  pause
  exit /b 1
)

echo [5/6] Installo/aggiorno dipendenze minime...
python -m pip install --upgrade pip >nul
python -m pip install -q python-telegram-bot==21.6

echo [6/6] Avvio bot...
REM Se usi ADMIN_IDS hai modificato il codice per supportarli (come ti ho spiegato).
python bot_slots_flow.py

REM Se il bot termina, tieni aperta la finestra per leggere eventuali errori
echo.
echo Bot terminato. Premi un tasto per chiudere...
pause >nul
