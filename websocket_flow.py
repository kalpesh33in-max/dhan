import json
import struct
import threading
import time

import pandas as pd
import websocket

from env_config import DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID
from instrument_store import exchange_code_for_segment, get_resolver

QUOTE_REQUEST_CODE = 17

_cache_lock = threading.Lock()
_token_quotes = {}
_symbol_quotes = {}
_meta = {
    "connected": False,
    "last_tick_time": 0.0,
}


def mark_connected(is_connected):
    with _cache_lock:
        _meta["connected"] = bool(is_connected)
        if is_connected:
            _meta["last_tick_time"] = time.time()


def update_token_quote(token, quote):
    if token is None or not isinstance(quote, dict):
        return

    now = time.time()
    key = str(token)
    payload = dict(quote)
    payload["ts"] = now

    with _cache_lock:
        existing = dict(_token_quotes.get(key, {}))
        existing.update(payload)
        _token_quotes[key] = existing
        _meta["last_tick_time"] = now


def update_symbol_quote(symbol, quote):
    if not symbol or not isinstance(quote, dict):
        return

    now = time.time()
    payload = dict(quote)
    payload["ts"] = now

    with _cache_lock:
        existing = dict(_symbol_quotes.get(symbol, {}))
        existing.update(payload)
        _symbol_quotes[symbol] = existing
        _meta["last_tick_time"] = now


def get_token_quotes(tokens, max_age_seconds=15):
    now = time.time()
    fresh = {}
    wanted = {str(token) for token in tokens}

    with _cache_lock:
        for token in wanted:
            data = _token_quotes.get(token)
            if data and "last_price" in data and now - data.get("ts", 0) <= max_age_seconds:
                fresh[token] = dict(data)
    return fresh


def get_symbol_quotes(symbols, max_age_seconds=15):
    now = time.time()
    fresh = {}

    with _cache_lock:
        for symbol in symbols:
            data = _symbol_quotes.get(symbol)
            if data and "last_price" in data and now - data.get("ts", 0) <= max_age_seconds:
                fresh[symbol] = dict(data)
    return fresh


class FlowEngine:
    def __init__(self, dhan_client, client_id=None, access_token=None, tokens=None):
        self.dhan_client = dhan_client
        self.client_id = client_id or DHAN_CLIENT_ID
        self.access_token = access_token or DHAN_ACCESS_TOKEN
        self.ws = None
        self._started = False
        self._stop_event = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._tokens = list(tokens or [])
        self._symbol_by_token = {}

    def start(self):
        with self._lock:
            if self._started:
                print("Dhan WebSocket collector already running. Skipping duplicate start.")
                return True

            if not self.client_id or not self.access_token:
                print("Dhan WebSocket collector not started: credentials missing.")
                return False

            tokens, symbol_by_token = self._build_subscription_map()
            if not tokens:
                print("Dhan WebSocket collector not started: no tokens selected.")
                return False

            self._tokens = tokens
            self._symbol_by_token = symbol_by_token
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
            self._started = True
            print(f"Dhan WebSocket collector started with {len(tokens)} subscriptions.")
            return True

    def stop(self):
        self._stop_event.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

    def _build_subscription_map(self):
        from heatmap_engine import BURST_TRACK_NAMES, get_bank_futures, get_relevant_options, load_futures_data, load_options_data

        if self._tokens:
            return self._tokens, self._symbol_by_token

        futures = load_futures_data()
        options = load_options_data()
        if futures is None or futures.empty or options is None or options.empty:
            return [], {}

        resolver = get_resolver()
        tokens = set()
        symbol_by_token = {}

        fut_symbols = get_bank_futures(self.dhan_client)
        if not fut_symbols:
            return [], {}

        fut_by_name = {}
        for symbol in fut_symbols:
            parts = symbol.split(":", 1)
            if len(parts) != 2:
                continue
            tsym = parts[1]
            for name in BURST_TRACK_NAMES:
                if tsym.startswith(name):
                    fut_by_name[name] = symbol

        symbol_quotes = {}
        try:
            symbol_quotes = self.dhan_client.quote(fut_symbols)
        except Exception as e:
            print(f"Dhan WebSocket bootstrap quote failed: {e}")

        for symbol in fut_symbols:
            row = resolver.resolve(symbol)
            if not row:
                continue
            token = int(row["instrument_token"])
            tokens.add(token)
            symbol_by_token[token] = symbol

        for name in BURST_TRACK_NAMES:
            base_symbol = fut_by_name.get(name, "")
            u_ltp = symbol_quotes.get(base_symbol, {}).get("last_price", 0)
            if u_ltp <= 0:
                continue

            df = get_relevant_options(name, u_ltp)
            if df.empty:
                continue

            for token in df["instrument_token"].tolist():
                token = int(token)
                tokens.add(token)
                symbol = resolver.symbol_for_token(token)
                if symbol:
                    symbol_by_token[token] = symbol

        return sorted(tokens), symbol_by_token

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                url = (
                    "wss://api-feed.dhan.co"
                    f"?version=2&token={self.access_token}&clientId={self.client_id}&authType=2"
                )
                self.ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print(f"Dhan WebSocket loop error: {e}")

            mark_connected(False)
            if not self._stop_event.is_set():
                time.sleep(3)

    def _on_open(self, ws):
        mark_connected(True)
        print("Dhan WebSocket connected.")
        self._subscribe(ws)

    def _subscribe(self, ws):
        resolver = get_resolver()
        instruments = []
        for token in self._tokens:
            row = resolver.resolve(token)
            if not row:
                continue
            exchange_segment = row["exchange_segment"]
            if exchange_code_for_segment(exchange_segment) is None:
                continue
            instruments.append(
                {
                    "ExchangeSegment": exchange_segment,
                    "SecurityId": str(int(row["instrument_token"])),
                }
            )

        for i in range(0, len(instruments), 100):
            chunk = instruments[i:i + 100]
            message = {
                "RequestCode": QUOTE_REQUEST_CODE,
                "InstrumentCount": len(chunk),
                "InstrumentList": chunk,
            }
            ws.send(json.dumps(message))
            time.sleep(0.2)

    def _on_message(self, ws, message):
        if not isinstance(message, (bytes, bytearray)):
            return
        parsed = self._parse_packet(message)
        if not parsed:
            return

        token = str(parsed.pop("instrument_token"))
        update_token_quote(token, parsed)

        symbol = self._symbol_by_token.get(int(token)) if token.isdigit() else None
        if symbol:
            update_symbol_quote(symbol, parsed)

    def _on_error(self, ws, error):
        print(f"Dhan WebSocket error: {error}")

    def _on_close(self, ws, code, reason):
        mark_connected(False)
        print(f"Dhan WebSocket closed: {code} {reason}")

    def _parse_packet(self, data):
        if len(data) < 8:
            return None
        response_code, _, _, security_id = struct.unpack_from("<BHBI", data, 0)
        token = str(security_id)

        try:
            if response_code == 2 and len(data) >= 16:  # Ticker packet
                return {
                    "instrument_token": token,
                    "last_price": struct.unpack_from("<f", data, 8)[0],
                    "timestamp": struct.unpack_from("<i", data, 12)[0],
                }

            if response_code == 4 and len(data) >= 50:  # Quote packet
                return {
                    "instrument_token": token,
                    "last_price": struct.unpack_from("<f", data, 8)[0],
                    "last_quantity": struct.unpack_from("<h", data, 12)[0],
                    "timestamp": struct.unpack_from("<i", data, 14)[0],
                    "average_price": struct.unpack_from("<f", data, 18)[0],
                    "volume": struct.unpack_from("<i", data, 22)[0],
                    "sell_quantity": struct.unpack_from("<i", data, 26)[0],
                    "buy_quantity": struct.unpack_from("<i", data, 30)[0],
                    "ohlc": {
                        "open": struct.unpack_from("<f", data, 34)[0],
                        "close": struct.unpack_from("<f", data, 38)[0],
                        "high": struct.unpack_from("<f", data, 42)[0],
                        "low": struct.unpack_from("<f", data, 46)[0],
                    },
                }

            if response_code == 5 and len(data) >= 12:  # OI packet
                return {
                    "instrument_token": token,
                    "oi": struct.unpack_from("<i", data, 8)[0],
                }

            if response_code == 6 and len(data) >= 16:  # Previous close packet
                return {
                    "instrument_token": token,
                    "ohlc": {"close": struct.unpack_from("<f", data, 8)[0]},
                    "prev_oi": struct.unpack_from("<i", data, 12)[0],
                }

            if response_code == 8 and len(data) >= 62:  # Full packet
                return {
                    "instrument_token": token,
                    "last_price": struct.unpack_from("<f", data, 8)[0],
                    "last_quantity": struct.unpack_from("<h", data, 12)[0],
                    "timestamp": struct.unpack_from("<i", data, 14)[0],
                    "average_price": struct.unpack_from("<f", data, 18)[0],
                    "volume": struct.unpack_from("<i", data, 22)[0],
                    "sell_quantity": struct.unpack_from("<i", data, 26)[0],
                    "buy_quantity": struct.unpack_from("<i", data, 30)[0],
                    "oi": struct.unpack_from("<i", data, 34)[0],
                    "oi_day_high": struct.unpack_from("<i", data, 38)[0],
                    "oi_day_low": struct.unpack_from("<i", data, 42)[0],
                    "ohlc": {
                        "open": struct.unpack_from("<f", data, 46)[0],
                        "close": struct.unpack_from("<f", data, 50)[0],
                        "high": struct.unpack_from("<f", data, 54)[0],
                        "low": struct.unpack_from("<f", data, 58)[0],
                    },
                }
        except Exception as e:
            print(f"Dhan packet parse error: {e}")

        return None
