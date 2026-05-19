import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

SECURITY_FILE = "security_id_list.csv"
COMPACT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
DETAILED_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"

IST = ZoneInfo("Asia/Kolkata")

_raw_df = None
_normalised_df = None
_resolver = None

INDEX_ALIASES = {
    "BANK NIFTY": "BANKNIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTY 50": "NIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "NIFTY FINANCIAL SERVICES": "FINNIFTY",
    "NIFTY MID SELECT": "MIDCPNIFTY",
    "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
    "BSE SENSEX": "SENSEX",
    "S&P BSE SENSEX": "SENSEX",
}

API_EXCHANGE_TO_WS_CODE = {
    "IDX_I": 0,
    "NSE_EQ": 1,
    "NSE_FNO": 2,
    "NSE_CURRENCY": 3,
    "BSE_EQ": 4,
    "MCX_COMM": 5,
    "BSE_CURRENCY": 7,
    "BSE_FNO": 8,
}


def reset_cache():
    global _raw_df, _normalised_df, _resolver
    _raw_df = None
    _normalised_df = None
    _resolver = None


def download_security_master(filename=SECURITY_FILE, detailed=False, timeout=60):
    url = DETAILED_MASTER_URL if detailed else COMPACT_MASTER_URL
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    with open(filename, "wb") as f:
        f.write(response.content)
    reset_cache()
    return filename


def _read_first_existing(df, names, default=""):
    for name in names:
        if name in df.columns:
            return df[name]
    return pd.Series([default] * len(df), index=df.index)


def _clean_string(series):
    return series.fillna("").astype(str).str.strip()


def _normalise_name(value):
    text = str(value or "").strip().upper()
    if not text or text in {"NAN", "NONE"}:
        return ""
    text = " ".join(text.replace("_", " ").replace("-", " ").split())
    return INDEX_ALIASES.get(text, text.replace(" ", ""))


def _infer_underlying_name(instrument, underlying, symbol_name, display_symbol, trading_symbol):
    instrument = str(instrument or "").strip().upper()
    candidates = []

    if instrument in {"FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK"}:
        candidates.extend(
            [
                str(display_symbol or "").strip().split(" ")[0],
                str(trading_symbol or "").strip().split("-")[0],
                underlying,
                symbol_name,
            ]
        )
    else:
        candidates.extend([underlying, symbol_name, trading_symbol, display_symbol])

    for candidate in candidates:
        normalised = _normalise_name(candidate)
        if normalised:
            return normalised
    return ""


def _derive_exchange_segment(exchange, segment, instrument):
    exchange = str(exchange or "").strip().upper()
    segment = str(segment or "").strip().upper()
    instrument = str(instrument or "").strip().upper()

    if instrument == "INDEX" or segment == "I":
        return "IDX_I"
    if exchange == "MCX":
        return "MCX_COMM"
    if instrument in {"FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK"} or segment == "D":
        return "BSE_FNO" if exchange == "BSE" else "NSE_FNO"
    if instrument in {"FUTCUR", "OPTCUR"} or segment == "C":
        return "BSE_CURRENCY" if exchange == "BSE" else "NSE_CURRENCY"
    if exchange == "BSE":
        return "BSE_EQ"
    return "NSE_EQ"


def _derive_kite_segment(exchange_segment, instrument):
    instrument = str(instrument or "").strip().upper()
    if exchange_segment == "IDX_I":
        return "INDICES"
    if exchange_segment == "BSE_FNO":
        return "BFO-OPT" if instrument.startswith("OPT") else "BFO-FUT"
    if exchange_segment == "NSE_FNO":
        return "NFO-OPT" if instrument.startswith("OPT") else "NFO-FUT"
    if exchange_segment in {"NSE_CURRENCY", "BSE_CURRENCY"}:
        return "CDS-OPT" if instrument.startswith("OPT") else "CDS-FUT"
    return "BSE" if exchange_segment == "BSE_EQ" else "NSE"


def _load_raw_security_master(filename=SECURITY_FILE):
    global _raw_df
    if _raw_df is not None:
        return _raw_df

    if not os.path.exists(filename):
        raise FileNotFoundError(
            f"{filename} not found. Run update_instruments() or open /refresh-instruments first."
        )

    df = pd.read_csv(filename, low_memory=False)
    df.columns = [str(col).strip() for col in df.columns]
    _raw_df = df
    return _raw_df


def load_instruments_frame(filename=SECURITY_FILE):
    global _normalised_df
    if _normalised_df is not None:
        return _normalised_df

    df = _load_raw_security_master(filename)

    token = _clean_string(_read_first_existing(df, ["SEM_SMST_SECURITY_ID", "SECURITY_ID"]))
    exchange_raw = _clean_string(_read_first_existing(df, ["SEM_EXM_EXCH_ID", "EXCH_ID"]))
    segment_raw = _clean_string(_read_first_existing(df, ["SEM_SEGMENT", "SEGMENT"]))
    instrument = _clean_string(_read_first_existing(df, ["SEM_INSTRUMENT_NAME", "INSTRUMENT"]))
    instrument_type = _clean_string(_read_first_existing(df, ["SEM_EXCH_INSTRUMENT_TYPE", "INSTRUMENT_TYPE"]))
    trading_symbol = _clean_string(
        _read_first_existing(df, ["SEM_TRADING_SYMBOL", "SYMBOL_NAME", "DISPLAY_NAME", "SEM_CUSTOM_SYMBOL"])
    )
    display_symbol = _clean_string(_read_first_existing(df, ["SEM_CUSTOM_SYMBOL", "DISPLAY_NAME", "SYMBOL_NAME"]))
    symbol_name = _clean_string(_read_first_existing(df, ["SM_SYMBOL_NAME", "SYMBOL_NAME", "UNDERLYING_SYMBOL"]))
    underlying_symbol = _clean_string(_read_first_existing(df, ["UNDERLYING_SYMBOL", "SM_SYMBOL_NAME", "SYMBOL_NAME"]))

    expiry_raw = _read_first_existing(df, ["SEM_EXPIRY_DATE", "SM_EXPIRY_DATE"])
    expiry = pd.to_datetime(expiry_raw, errors="coerce").dt.normalize()

    strike = pd.to_numeric(_read_first_existing(df, ["SEM_STRIKE_PRICE", "STRIKE_PRICE"], 0), errors="coerce").fillna(0)
    lot_size = pd.to_numeric(_read_first_existing(df, ["SEM_LOT_UNITS", "LOT_SIZE"], 1), errors="coerce").fillna(1)
    tick_size = pd.to_numeric(_read_first_existing(df, ["SEM_TICK_SIZE", "TICK_SIZE"], 0.05), errors="coerce").fillna(0.05)
    option_type = _clean_string(_read_first_existing(df, ["SEM_OPTION_TYPE", "OPTION_TYPE"]))

    exchange_segment = [
        _derive_exchange_segment(exchange_raw.iloc[i], segment_raw.iloc[i], instrument.iloc[i])
        for i in range(len(df))
    ]
    kite_segment = [
        _derive_kite_segment(exchange_segment[i], instrument.iloc[i])
        for i in range(len(df))
    ]

    name_values = []
    for i in range(len(df)):
        name_values.append(
            _infer_underlying_name(
                instrument.iloc[i],
                underlying_symbol.iloc[i],
                symbol_name.iloc[i],
                display_symbol.iloc[i],
                trading_symbol.iloc[i],
            )
        )

    out = pd.DataFrame(
        {
            "instrument_token": pd.to_numeric(token, errors="coerce"),
            "exchange_token": pd.to_numeric(token, errors="coerce"),
            "tradingsymbol": trading_symbol,
            "display_name": display_symbol,
            "name": name_values,
            "last_price": 0,
            "expiry": expiry,
            "strike": strike,
            "tick_size": tick_size,
            "lot_size": lot_size.astype(int),
            "instrument_type": instrument,
            "dhan_instrument_type": instrument,
            "option_type": option_type,
            "segment": kite_segment,
            "exchange": exchange_segment,
            "exchange_segment": exchange_segment,
            "exchange_raw": exchange_raw,
        }
    )
    out = out[out["instrument_token"].notna()].copy()
    out["instrument_token"] = out["instrument_token"].astype(int)
    out["exchange_token"] = out["exchange_token"].astype(int)

    # Keep alert classification compatible with Zerodha symbols.
    opt_mask = out["instrument_type"].isin(["OPTIDX", "OPTSTK"])
    out.loc[opt_mask & out["option_type"].isin(["CE", "PE"]), "tradingsymbol"] = (
        out.loc[opt_mask & out["option_type"].isin(["CE", "PE"]), "tradingsymbol"].astype(str)
    )

    _normalised_df = out
    return _normalised_df


def _key(exchange_segment, text):
    return f"{str(exchange_segment).upper()}:{str(text).strip().upper()}"


class InstrumentResolver:
    def __init__(self, df=None):
        self.df = df if df is not None else load_instruments_frame()
        self.by_token = {}
        self.by_key = {}
        self._build()

    def _add_key(self, exchange_segment, text, row):
        text = str(text or "").strip()
        if not text:
            return
        self.by_key.setdefault(_key(exchange_segment, text), row)
        self.by_key.setdefault(_key(exchange_segment, _normalise_name(text)), row)

    def _build(self):
        for _, item in self.df.iterrows():
            row = item.to_dict()
            token = str(int(row["instrument_token"]))
            self.by_token[token] = row
            exchange_segment = row.get("exchange_segment") or row.get("exchange")
            self._add_key(exchange_segment, row.get("tradingsymbol"), row)
            self._add_key(exchange_segment, row.get("display_name"), row)
            self._add_key(exchange_segment, row.get("name"), row)

    def resolve(self, value):
        if value is None:
            return None

        if isinstance(value, (int, float)) and not pd.isna(value):
            return self.by_token.get(str(int(value)))

        text = str(value).strip()
        if not text:
            return None

        if text.isdigit():
            return self.by_token.get(text)

        if ":" in text:
            exchange_segment, symbol = text.split(":", 1)
            exchange_segment = exchange_segment.strip().upper()
            symbol = symbol.strip()
            row = self.by_key.get(_key(exchange_segment, symbol))
            if row:
                return row

            # Allow NSE_EQ:RELIANCE style spot lookup by normalised name.
            normalised_symbol = _normalise_name(symbol)
            candidates = self.df[
                (self.df["exchange_segment"].str.upper() == exchange_segment)
                & (self.df["name"].str.upper() == normalised_symbol)
            ]
            if not candidates.empty:
                return candidates.iloc[0].to_dict()

        normalised = _normalise_name(text)
        candidates = self.df[self.df["name"].str.upper() == normalised]
        if not candidates.empty:
            return candidates.iloc[0].to_dict()
        return None

    def symbol_for_token(self, token):
        row = self.by_token.get(str(int(token))) if str(token).isdigit() else None
        if not row:
            return None
        return f"{row['exchange_segment']}:{row['tradingsymbol']}"


def get_resolver():
    global _resolver
    if _resolver is None:
        _resolver = InstrumentResolver()
    return _resolver


def exchange_code_for_segment(exchange_segment):
    return API_EXCHANGE_TO_WS_CODE.get(str(exchange_segment).upper())
