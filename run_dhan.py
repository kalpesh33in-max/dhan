import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask

from dhan_client import DhanLikeClient
from env_config import DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID
from instrument_store import SECURITY_FILE, download_security_master, load_instruments_frame, reset_cache
from scanner import run_scanner
from websocket_flow import FlowEngine

IST = ZoneInfo("Asia/Kolkata")
AUTO_START_SCANNER = os.getenv("AUTO_START_SCANNER", "true").lower() in ("true", "1", "yes")
AUTO_START_BACKGROUND = os.getenv("AUTO_START_BACKGROUND", "true").lower() in ("true", "1", "yes")
INSTRUMENT_UPDATE_MODE = os.getenv("INSTRUMENT_UPDATE_MODE", "monthly").lower()
INSTRUMENT_UPDATE_TIME = os.getenv("INSTRUMENT_UPDATE_TIME", "08:30")

app = Flask(__name__)
dhan = DhanLikeClient()

scanner_thread = None
flow_engine = None
scanner_lock = threading.Lock()
background_started = False


def credentials_present():
    return (
        DHAN_CLIENT_ID
        and DHAN_CLIENT_ID != "YOUR_DHAN_CLIENT_ID"
        and DHAN_ACCESS_TOKEN
        and DHAN_ACCESS_TOKEN != "YOUR_DHAN_ACCESS_TOKEN"
    )


def ensure_instruments_available(source):
    if os.path.exists(SECURITY_FILE):
        return True
    print(f"[{source}] {SECURITY_FILE} missing. Downloading Dhan security master...")
    try:
        download_security_master(SECURITY_FILE)
        load_instruments_frame()
        return True
    except Exception as e:
        print(f"[{source}] Dhan instrument download failed: {e}")
        return False


def validate_and_start_scanner(source):
    global scanner_thread, flow_engine
    with scanner_lock:
        if scanner_thread and scanner_thread.is_alive():
            print(f"[{source}] Dhan scanner already running.")
            return True

        if not credentials_present():
            print(f"[{source}] Dhan credentials missing. Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN.")
            return False

        if not ensure_instruments_available(source):
            return False

        try:
            dhan.profile()
            print(f"[{source}] Dhan credentials validated. Starting engine...")

            flow_engine = FlowEngine(dhan)
            flow_engine.start()

            scanner_thread = threading.Thread(target=run_scanner, args=(dhan,), daemon=True)
            scanner_thread.start()
            return True
        except Exception as e:
            print(f"[{source}] Dhan validation failed: {e}")
            return False


def update_instruments():
    print("Updating Dhan security_id_list.csv...")
    try:
        download_security_master(SECURITY_FILE)
        reset_cache()
        load_instruments_frame()
        print("Dhan instruments updated.")
    except Exception as e:
        print(f"Dhan instrument update error: {e}")


def _configured_update_time():
    try:
        return datetime.strptime(INSTRUMENT_UPDATE_TIME, "%H:%M").time()
    except ValueError:
        print(f"Invalid INSTRUMENT_UPDATE_TIME={INSTRUMENT_UPDATE_TIME!r}; using 08:30.")
        return datetime.strptime("08:30", "%H:%M").time()


def _last_weekday_of_month(now):
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1)
    else:
        next_month = now.replace(month=now.month + 1, day=1)

    last_day = (next_month - timedelta(days=1)).date()
    while last_day.weekday() > 4:
        last_day -= timedelta(days=1)
    return last_day


def _instrument_update_due(now, last_update_key):
    if INSTRUMENT_UPDATE_MODE in {"off", "false", "0", "no"}:
        return False, last_update_key

    if now.weekday() > 4 or now.time() < _configured_update_time():
        return False, last_update_key

    if INSTRUMENT_UPDATE_MODE == "daily":
        update_key = now.date().isoformat()
        return update_key != last_update_key, update_key

    update_day = _last_weekday_of_month(now)
    update_key = f"{now.year}-{now.month:02d}"
    if now.date() == update_day and update_key != last_update_key:
        return True, update_key

    return False, last_update_key


def scheduled_instrument_task():
    now = datetime.now(IST)
    print(f"Dhan instrument update task started at {now.strftime('%Y-%m-%d %H:%M')}")
    update_instruments()


def run_scheduler_loop():
    print(
        "Dhan background scheduler active. "
        f"Instrument update mode={INSTRUMENT_UPDATE_MODE}, time={INSTRUMENT_UPDATE_TIME} IST."
    )
    last_instrument_update_key = None

    while True:
        now = datetime.now(IST)
        due, update_key = _instrument_update_due(now, last_instrument_update_key)
        if due:
            scheduled_instrument_task()
            last_instrument_update_key = update_key
        time.sleep(10)


def start_background_services(source):
    global background_started
    with scanner_lock:
        if background_started:
            print(f"[{source}] Dhan background services already started.")
            return
        background_started = True

    print(f"[{source}] Starting Dhan background tasks...")
    sched_thread = threading.Thread(target=run_scheduler_loop, daemon=True)
    sched_thread.start()

    if AUTO_START_SCANNER:
        boot_thread = threading.Thread(
            target=validate_and_start_scanner,
            args=(source,),
            daemon=True,
        )
        boot_thread.start()


def ensure_background_services_started(source):
    if AUTO_START_BACKGROUND:
        start_background_services(source)


@app.route("/")
def home():
    ensure_background_services_started("HTTP /")
    status = "RUNNING" if (scanner_thread and scanner_thread.is_alive()) else "STOPPED"
    return (
        f"<h3>Dhan Scanner Status: {status}</h3>"
        f"<p>Server Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}</p>"
        f"<p>Instrument Update: {INSTRUMENT_UPDATE_MODE} at {INSTRUMENT_UPDATE_TIME} IST</p>"
    )


@app.route("/start")
def start():
    ensure_background_services_started("HTTP /start")
    ok = validate_and_start_scanner("Manual Start")
    return "<h1>Dhan Scanner Started</h1>" if ok else "<h1>Dhan Scanner Start Failed</h1>"


@app.route("/refresh-instruments")
def refresh_instruments():
    ensure_background_services_started("HTTP /refresh-instruments")
    update_instruments()
    return "<h1>Dhan instruments refresh requested.</h1>"


@app.route("/login")
def login():
    return (
        "<h3>Dhan uses environment token login</h3>"
        "<p>Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN, then open /start.</p>"
    )


if __name__ == "__main__":
    print(f"Starting Dhan Flask dev server on port {os.getenv('PORT', 8080)}...")
    start_background_services("Direct Run")
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
