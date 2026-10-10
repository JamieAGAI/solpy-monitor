import pandas as pd
from binance.client import Client
from datetime import datetime
import time
import os

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
client = Client(requests_params={"verify": False})

UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "LTCUSDT", "BCHUSDT", "ATOMUSDT", "TRXUSDT", "XLMUSDT",
    "ETCUSDT", "NEARUSDT", "FILUSDT", "APTUSDT", "ARBUSDT",
]

FUNDING_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

DEFAULT_START = "1 January 2020"

DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)


def fetch_spot_daily(symbol, start=DEFAULT_START):
    print(f"  spot daily {symbol} from {start}...")
    all_data = []
    start_ts = int(pd.Timestamp(start).timestamp() * 1000)
    while True:
        try:
            klines = client.get_historical_klines(symbol, Client.KLINE_INTERVAL_1DAY, start_ts, limit=1000)
        except Exception as e:
            print(f"  ERROR {symbol}: {e}")
            break
        if not klines:
            break
        all_data.extend(klines)
        start_ts = klines[-1][0] + 1
        if len(klines) < 1000:
            break
        time.sleep(0.3)

    if not all_data:
        print(f"  no data for {symbol}")
        return None

    df = pd.DataFrame(all_data, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trades', 'taker_base', 'taker_quote', 'ignore'
    ])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms').dt.normalize()
    df.set_index('timestamp', inplace=True)
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col])
    df = df[['open', 'high', 'low', 'close', 'volume']]

    path = os.path.join(DATA_DIR, f"{symbol}_1d.csv")
    df.to_csv(path)
    print(f"  -> {len(df)} bars saved to {path}")
    return df


def fetch_perp_daily(symbol, start=DEFAULT_START):
    """Futures/perp daily klines for delta-neutral basis."""
    print(f"  perp daily {symbol} from {start}...")
    all_data = []
    start_ts = int(pd.Timestamp(start).timestamp() * 1000)
    while True:
        try:
            klines = client.futures_klines(symbol=symbol, interval="1d", startTime=start_ts, limit=1000)
        except Exception as e:
            print(f"  ERROR perp {symbol}: {e}")
            break
        if not klines:
            break
        all_data.extend(klines)
        start_ts = klines[-1][0] + 1
        if len(klines) < 1000:
            break
        time.sleep(0.3)

    if not all_data:
        return None

    df = pd.DataFrame(all_data, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trades', 'taker_base', 'taker_quote', 'ignore'
    ])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms').dt.normalize()
    df.set_index('timestamp', inplace=True)
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col])
    df = df[['open', 'high', 'low', 'close', 'volume']]

    path = os.path.join(DATA_DIR, f"{symbol}_perp_1d.csv")
    df.to_csv(path)
    print(f"  -> {len(df)} bars saved to {path}")
    return df


def fetch_funding(symbol, start=DEFAULT_START):
    """8-hourly funding rates for a perp symbol."""
    print(f"  funding {symbol} from {start}...")
    all_data = []
    start_ts = int(pd.Timestamp(start).timestamp() * 1000)
    while True:
        try:
            rates = client.futures_funding_rate(symbol=symbol, startTime=start_ts, limit=1000)
        except Exception as e:
            print(f"  ERROR funding {symbol}: {e}")
            break
        if not rates:
            break
        all_data.extend(rates)
        start_ts = rates[-1]['fundingTime'] + 1
        if len(rates) < 1000:
            break
        time.sleep(0.3)

    if not all_data:
        return None

    df = pd.DataFrame(all_data)
    df['fundingTime'] = pd.to_datetime(df['fundingTime'], unit='ms')
    df['fundingRate'] = pd.to_numeric(df['fundingRate'])
    df = df[['fundingTime', 'fundingRate']].set_index('fundingTime')
    df.index.name = 'timestamp'

    path = os.path.join(DATA_DIR, f"{symbol}_funding.csv")
    df.to_csv(path)
    print(f"  -> {len(df)} funding events saved to {path}")
    return df


if __name__ == "__main__":
    print("=== Fetching spot daily universe ===")
    for sym in UNIVERSE:
        fetch_spot_daily(sym)
        time.sleep(0.5)

    print("\n=== Fetching perp daily (for funding harvest) ===")
    for sym in FUNDING_SYMBOLS:
        fetch_perp_daily(sym)
        time.sleep(0.5)

    print("\n=== Fetching funding rates ===")
    for sym in FUNDING_SYMBOLS:
        fetch_funding(sym)
        time.sleep(0.5)

    print("\nDone.")
