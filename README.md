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
- `/stop` manually stop scanner
- `/refresh-instruments` refresh Dhan security master
- `/login` setup note; Dhan uses environment token login

The scanner downloads `security_id_list.csv` from Dhan automatically if it is missing.

During scanner operation, futures/options burst alerts plus scanner-start and scanner-stop messages are sent to Telegram. Gap, pivot, reversal, weekly breakout, and scanner error Telegram messages are disabled in the Dhan scanner.

Crude oil burst alerts are additionally checked after 15:30 IST on weekdays, using the same 15-second OI burst watch logic with crude-specific strength levels:

- 25+ lots = GOOD
- 50+ lots = VERY GOOD
- 100+ lots = AWESOME
- 200+ lots = BLAST

Instrument refresh success/failure messages are sent to Telegram for scheduled refreshes and for manual `/refresh-instruments` requests.

## Instrument List Auto Update

For daily pre-market refreshes on Railway, set `INSTRUMENT_UPDATE_MODE=daily` and `INSTRUMENT_UPDATE_TIME=08:30`. The refresh runs once per weekday at or after the configured IST time.

Optional scheduler settings:

```powershell
$env:INSTRUMENT_UPDATE_MODE="daily"     # daily, monthly, or off
$env:INSTRUMENT_UPDATE_TIME="08:30"     # HH:mm IST
```
