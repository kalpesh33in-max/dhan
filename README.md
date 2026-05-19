# Dhan Scanner

This is a DhanHQ futures/options burst scanner, kept separate from `C:\Users\kalpe\zarodha`.

## Required Environment

Set these before running:

```powershell
$env:DHAN_CLIENT_ID="your_dhan_client_id"
$env:DHAN_ACCESS_TOKEN="your_dhan_access_token"
$env:TELEGRAM_TOKEN="your_telegram_bot_token"
$env:CHAT_ID="your_default_chat_id"
```

Optional channel overrides:

```powershell
$env:TELE_TOKEN_BN="banknifty_channel_bot_token"
$env:CHAT_ID_BN="banknifty_channel_chat_id"
$env:TELE_TOKEN_STOCKS="stocks_channel_bot_token"
$env:CHAT_ID_STOCKS="stocks_channel_chat_id"
```

## Run

```powershell
cd C:\Users\kalpe\dhan
pip install -r requirements.txt
python run_dhan.py
```

Routes:

- `/` scanner status
- `/start` manually start scanner
- `/refresh-instruments` refresh Dhan security master
- `/login` setup note; Dhan uses environment token login

The scanner downloads `security_id_list.csv` from Dhan automatically if it is missing.

Only futures and options burst alerts are sent to Telegram. Gap, pivot, reversal, weekly breakout, startup, stop, error, and instrument-update Telegram messages are disabled in the Dhan scanner.

## Instrument List Auto Update

By default, `run_dhan.py` refreshes `security_id_list.csv` automatically on the last weekday of every month at 08:30 IST.

Optional scheduler settings:

```powershell
$env:INSTRUMENT_UPDATE_MODE="monthly"   # monthly, daily, or off
$env:INSTRUMENT_UPDATE_TIME="08:30"     # HH:mm IST
```
