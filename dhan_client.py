import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from env_config import DHAN_ACCESS_TOKEN, DHAN_CLIENT_ID
from instrument_store import get_resolver

IST = ZoneInfo("Asia/Kolkata")


class DhanLikeClient:
    """Small adapter exposing the quote/historical methods used by the scanner."""

    def __init__(self, client_id=None, access_token=None):
        self.client_id = client_id or DHAN_CLIENT_ID
        self.access_token = access_token or DHAN_ACCESS_TOKEN
        self._dhan = None
        self._quote_lock = threading.Lock()
        self._last_quote_call = 0.0

    def _client(self):
        if self._dhan is None:
            from dhanhq import DhanContext, dhanhq

            context = DhanContext(self.client_id, self.access_token)
            self._dhan = dhanhq(context)
        return self._dhan

    def profile(self):
        if not self.client_id or self.client_id == "YOUR_DHAN_CLIENT_ID":
            raise ValueError("DHAN_CLIENT_ID is missing")
        if not self.access_token or self.access_token == "YOUR_DHAN_ACCESS_TOKEN":
            raise ValueError("DHAN_ACCESS_TOKEN is missing")

        dhan = self._client()
        # A light validation call if the installed client exposes it. Different
        # DhanHQ versions name account APIs differently, so keep validation tolerant.
        for method_name in ("get_fund_limits", "get_positions"):
            method = getattr(dhan, method_name, None)
            if callable(method):
                try:
                    return method()
                except Exception:
                    raise
        return {"client_id": self.client_id}

    def _throttle_quote_api(self):
        with self._quote_lock:
            elapsed = time.time() - self._last_quote_call
            if elapsed < 1.05:
                time.sleep(1.05 - elapsed)
            self._last_quote_call = time.time()

    def _group_symbols(self, symbols):
        resolver = get_resolver()
        grouped = {}
        lookup = {}
        for original in symbols:
            row = resolver.resolve(original)
            if not row:
                continue
            exchange_segment = row["exchange_segment"]
            token = str(int(row["instrument_token"]))
            grouped.setdefault(exchange_segment, []).append(int(token))
            lookup[(exchange_segment, token)] = original
        return grouped, lookup

    def quote(self, symbols):
        if not symbols:
            return {}
        if not isinstance(symbols, (list, tuple, set)):
            symbols = [symbols]

        grouped, lookup = self._group_symbols(symbols)
        if not grouped:
            return {}

        dhan = self._client()
        result = {}

        for exchange_segment, tokens in grouped.items():
            for i in range(0, len(tokens), 1000):
                chunk = tokens[i:i + 1000]
                self._throttle_quote_api()
                response = dhan.quote_data({exchange_segment: chunk})
                data = response.get("data", response) if isinstance(response, dict) else {}
                segment_data = data.get(exchange_segment, {}) if isinstance(data, dict) else {}
                for token, quote in segment_data.items():
                    original = lookup.get((exchange_segment, str(token)), token)
                    result[original] = self._normalise_quote(quote)
        return result

    def _normalise_quote(self, quote):
        if not isinstance(quote, dict):
            return {}
        ohlc = quote.get("ohlc") or {}
        return {
            "last_price": quote.get("last_price", quote.get("ltp", 0)) or 0,
            "oi": quote.get("oi", quote.get("open_interest", 0)) or 0,
            "ohlc": {
                "open": ohlc.get("open", 0) or 0,
                "high": ohlc.get("high", 0) or 0,
                "low": ohlc.get("low", 0) or 0,
                "close": ohlc.get("close", 0) or 0,
            },
            "volume": quote.get("volume", 0) or 0,
            "timestamp": quote.get("last_trade_time") or datetime.now(IST),
        }

    def historical_data(self, token, from_time, to_time, interval):
        resolver = get_resolver()
        row = resolver.resolve(token)
        if not row:
            return []

        interval_value = self._dhan_interval(interval)
        dhan = self._client()
        security_id = str(int(row["instrument_token"]))
        exchange_segment = row["exchange_segment"]
        instrument_type = row["dhan_instrument_type"]

        if interval == "day":
            response = dhan.historical_daily_data(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument_type,
                from_date=from_time.strftime("%Y-%m-%d"),
                to_date=to_time.strftime("%Y-%m-%d"),
                oi=True,
            )
            return self._normalise_candles(response)

        # Dhan v2 supports 1, 5, 15, 25 and 60 minute intervals. The Zerodha
        # scanner has a 30-minute pattern, so we build it from 15-minute candles.
        source_interval = 15 if interval_value == 30 else interval_value
        response = dhan.intraday_minute_data(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            from_date=from_time.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=to_time.strftime("%Y-%m-%d %H:%M:%S"),
            interval=source_interval,
            oi=True,
        )
        candles = self._normalise_candles(response)
        if interval_value == 30:
            return self._aggregate_candles(candles, 30)
        return candles

    def _dhan_interval(self, interval):
        mapping = {
            "minute": 1,
            "1minute": 1,
            "5minute": 5,
            "15minute": 15,
            "25minute": 25,
            "30minute": 30,
            "60minute": 60,
        }
        if interval == "day":
            return "day"
        return mapping.get(str(interval).lower(), 1)

    def _normalise_candles(self, response):
        payload = response.get("data", response) if isinstance(response, dict) else {}
        if not isinstance(payload, dict):
            return []

        opens = payload.get("open", [])
        highs = payload.get("high", [])
        lows = payload.get("low", [])
        closes = payload.get("close", [])
        volumes = payload.get("volume", [])
        timestamps = (
            payload.get("timestamp")
            or payload.get("start_Time")
            or payload.get("start_time")
            or payload.get("time")
            or payload.get("date")
            or []
        )
        oi_values = payload.get("oi", payload.get("open_interest", []))

        count = min(len(opens), len(highs), len(lows), len(closes))
        candles = []
        for i in range(count):
            candles.append(
                {
                    "date": self._parse_timestamp(timestamps[i] if i < len(timestamps) else None),
                    "open": float(opens[i] or 0),
                    "high": float(highs[i] or 0),
                    "low": float(lows[i] or 0),
                    "close": float(closes[i] or 0),
                    "volume": float(volumes[i] or 0) if i < len(volumes) else 0,
                    "oi": float(oi_values[i] or 0) if isinstance(oi_values, list) and i < len(oi_values) else 0,
                }
            )
        return [candle for candle in candles if candle["date"] is not None]

    def _parse_timestamp(self, value):
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value.astimezone(IST) if value.tzinfo else value.replace(tzinfo=IST)
        if isinstance(value, (int, float)) and not pd.isna(value):
            # Dhan returns epoch seconds for historical data.
            return datetime.fromtimestamp(float(value), IST)
        text = str(value)
        parsed = pd.to_datetime(text, errors="coerce")
        if pd.isna(parsed):
            return None
        dt = parsed.to_pydatetime()
        return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)

    def _aggregate_candles(self, candles, interval_minutes):
        buckets = {}
        for candle in candles:
            dt = candle["date"].astimezone(IST)
            session_open = dt.replace(hour=9, minute=15, second=0, microsecond=0)
            minutes = int((dt - session_open).total_seconds() // 60)
            if minutes < 0:
                continue
            bucket_start = session_open + pd.Timedelta(minutes=(minutes // interval_minutes) * interval_minutes)
            key = bucket_start
            current = buckets.get(key)
            if current is None:
                buckets[key] = dict(candle, date=key)
                continue
            current["high"] = max(current["high"], candle["high"])
            current["low"] = min(current["low"], candle["low"])
            current["close"] = candle["close"]
            current["volume"] += candle.get("volume", 0)
            current["oi"] = candle.get("oi", current.get("oi", 0))
        return [buckets[key] for key in sorted(buckets)]

