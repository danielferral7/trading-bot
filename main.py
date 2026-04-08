import datetime
import os
import time
import threading
import traceback
import random
import pandas as pd
import requests
from decimal import Decimal
from binance.client import Client

# ========= CONFIG =========
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TEST_MODE = False
AUTO_MODE = True
CHECK_POSITION = True
DEBUG = False

LEVERAGE = 5
USDT_AMOUNT = 2
LOSS_USD = 2
TRAIL_TRIGGER_USD = 0.5
TRAIL_LOCK_PERCENT = 0.75

# ========= SIGNAL CONFIG =========
DAILY_GREEN_CANDLES = 3
H4_RED_CANDLES = 2
M15_LOOKBACK = 2

# ===== NUEVO =====
PUMP_THRESHOLD = 0.6  # 100%
PUMP_LOOKBACK = 18      # velas 4H

# ========= LIQUIDITY FILTER =========
MIN_VOLUME_24H = 50_000_000   # 50M USDT (ajústalo)
EXCLUDE_SYMBOLS = {}  # opcional blacklist manual

active_orders = {}
client = Client(API_KEY, API_SECRET)

# ========= TELEGRAM =========
def send_telegram(msg):
    try:
        mode = "🧪 TEST" if TEST_MODE else "🚀 PROD"
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"{mode}\n{msg}"
        }, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

# ========= RETRY =========
def retry(max_retries=3):
    def decorator(func):
        def wrapper(*args, **kwargs):
            for i in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception:
                    time.sleep(2 ** i + random.random())
            return None
        return wrapper
    return decorator

# ========= UTILS =========
@retry()
def get_symbols():
    info = client.futures_exchange_info()
    return [s['symbol'] for s in info['symbols'] if s['quoteAsset'] == 'USDT']

@retry()
def get_price(symbol):
    return float(client.futures_mark_price(symbol=symbol)["markPrice"])

@retry()
def has_open_position(symbol):
    positions = client.futures_position_information(symbol=symbol)
    return any(abs(float(p["positionAmt"])) > 0 for p in positions)

@retry()
def get_klines(symbol, interval, limit):
    data = client.futures_klines(symbol=symbol, interval=interval, limit=limit)

    if not data or len(data) < limit:
        return None

    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume",
        "close_time","qav","trades","taker_base","taker_quote","ignore"
    ])

    for col in ["open","close","high","low"]:
        df[col] = df[col].astype(float)

    return df

@retry()
def filter_symbols_by_volume(symbols):
    tickers = client.futures_ticker()

    valid = []

    for t in tickers:
        symbol = t["symbol"]

        if symbol not in symbols:
            continue

        if symbol in EXCLUDE_SYMBOLS:
            continue

        volume = float(t["quoteVolume"])

        if volume >= MIN_VOLUME_24H:
            valid.append(symbol)
        else:
            if DEBUG:
                print(f"🚫 Low cap filtrado {symbol} | Vol: {volume:,.0f}")

    return valid

# ========= ATR =========
def get_atr(symbol, period=14):
    df = get_klines(symbol, Client.KLINE_INTERVAL_15MINUTE, period + 1)

    if df is None or len(df) < period:
        return None

    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()

    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]

    return float(atr) if not pd.isna(atr) else None

# ========= SWINGS =========
def get_swings(symbol):
    df = get_klines(symbol, Client.KLINE_INTERVAL_15MINUTE, 20)

    highs = df["high"].values
    lows = df["low"].values

    LH = []
    HL = []

    for i in range(2, len(df)-2):
        # swing high
        if highs[i] > highs[i-1] and highs[i] > highs[i+1]:
            LH.append(highs[i])

        # swing low
        if lows[i] < lows[i-1] and lows[i] < lows[i+1]:
            HL.append(lows[i])

    return {"LH": LH, "HL": HL}

# ========= FILTROS =========
def get_filters(symbol):
    info = client.futures_exchange_info()
    for s in info['symbols']:
        if s['symbol'] == symbol:
            step = tick = None
            for f in s['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    step = float(f['stepSize'])
                if f['filterType'] == 'PRICE_FILTER':
                    tick = float(f['tickSize'])
            return step, tick
    return None, None

# ========= PUMP =========
def detect_pump(symbol):
    df = get_klines(symbol, Client.KLINE_INTERVAL_4HOUR, PUMP_LOOKBACK)

    low = df["low"].min()
    high = df["high"].max()

    move = (high - low) / low
    return move

# ========= 15M CONFIRM =========
def confirm_15m_downtrend(symbol):
    df = get_klines(symbol, Client.KLINE_INTERVAL_15MINUTE, M15_LOOKBACK)

    if df is None or len(df) < M15_LOOKBACK:
        return False

    # todas las velas deben ser bajistas y con estructura descendente
    for i in range(1, M15_LOOKBACK):
        curr = df.iloc[i]
        prev = df.iloc[i - 1]

        if not (
            curr["close"] < curr["open"] and
            curr["high"] < prev["high"] and
            curr["low"] < prev["low"]
        ):
            return False

    return True

# ========= SIGNAL =========
def detect_signal(symbol):
    df_d = get_klines(symbol, Client.KLINE_INTERVAL_1DAY, DAILY_GREEN_CANDLES + 1)
    df_4h = get_klines(symbol, Client.KLINE_INTERVAL_4HOUR, H4_RED_CANDLES + 1)

    if df_d is None or df_4h is None:
        return False

    if len(df_d) < DAILY_GREEN_CANDLES or len(df_4h) < H4_RED_CANDLES:
        return False

    # =========================
    # 🟢 DIARIO
    # =========================

    move = detect_pump(symbol)   
    
    last_green = all(
        df_d["close"].iloc[-i] > df_d["open"].iloc[-i]
        for i in range(1, DAILY_GREEN_CANDLES + 1)
    )

    if last_green:
        print(f"+-→{symbol} - {DAILY_GREEN_CANDLES}V verdes diario. {move:.0%}")

    if not move >= PUMP_THRESHOLD: 
            return False

    # =========================
    # 🔴 4H
    # =========================
    last_red = all(
        df_4h["close"].iloc[-i] < df_4h["open"].iloc[-i]
        for i in range(1, H4_RED_CANDLES + 1)
    )

    if last_green and last_red:
        print(f"   └-→{symbol} - {H4_RED_CANDLES}V rojas 4H")

    if not (last_green and last_red):
        return False

    # =========================
    # ⬇️ 15M
    # =========================
    entry_downtrend = confirm_15m_downtrend(symbol)

    if entry_downtrend:
        print(f"      └-→{symbol} - {M15_LOOKBACK} velas bajistas 15m ✅")

    if not entry_downtrend:
        return False

    return True

def update_stop_loss(symbol, new_sl, qty):
    try:
        client.futures_create_order(
            symbol=symbol,
            side="BUY",
            type="STOP_MARKET",
            stopPrice=new_sl,
            quantity=qty,
            reduceOnly=True
        )

        print(f"🔄 SL actualizado {symbol}: {new_sl}")
        return True

    except Exception as e:
        print(f"SL error {symbol}: {e}")
        return False
        
def create_sl_with_retry(symbol, stop_price, retries=3):
    for i in range(retries):
        try:
            client.futures_create_order(
                symbol=symbol,
                side="BUY",
                type="STOP_MARKET",
                stopPrice=stop_price,
                closePosition=True
            )
            return True

        except Exception as e:
            if "-4130" in str(e):
                print(f"⏳ Esperando liberación SL {symbol}...")
                time.sleep(0.5)
                continue
            else:
                print(f"SL create error {symbol}: {e}")
                return False

    print(f"❌ No se pudo crear SL {symbol}")
    return False

# ========= CALCULOS =========
def calculate_qty(symbol, price):
    step, _ = get_filters(symbol)
    qty = (USDT_AMOUNT * LEVERAGE) / price
    return adjust_qty(qty, step)

def calculate_sl_price_short(entry_price, qty, loss_usd, tick):
    move = loss_usd / qty
    return adjust_price(entry_price + move, tick)

# ========= ORDERS =========
def open_short(symbol):
    price = get_price(symbol)
    qty = calculate_qty(symbol, price)

    if TEST_MODE:
        return price, qty

    client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)

    client.futures_create_order(
        symbol=symbol,
        side="SELL",
        type="MARKET",
        quantity=qty
    )

    return price, qty

def set_stop_loss(symbol, entry_price, qty):
    _, tick = get_filters(symbol)

    sl_price = calculate_sl_price_short(entry_price, qty, LOSS_USD, tick)
    current_price = get_price(symbol)

    if sl_price <= current_price:
        sl_price = current_price + (tick * 5)

    if TEST_MODE:
        return sl_price

    client.futures_create_order(
        symbol=symbol,
        side="BUY",
        type="STOP_MARKET",
        stopPrice=sl_price,
        quantity=qty,
        reduceOnly=True
    )

    send_telegram(f"🛑 SL inicial\n{symbol}\n{sl_price}")

    return sl_price

def clean_orders(symbol):
    try:
        open_orders = client.futures_get_open_orders(symbol=symbol)
        for o in open_orders:
            if o.get("closePosition"):
                client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
    except:
        pass

def update_trailing_sl(symbol, data):
    try:
        price = get_price(symbol)
        _, tick = get_filters(symbol)

        entry = data["entry"]
        qty = abs(data["qty"])

        # 🔥 VALIDACIÓN
        if qty == 0:
            print(f"⚠️ {symbol} sin posición")
            if symbol in active_orders:
                del active_orders[symbol]
            return

        current_sl = data.get("sl")

        # 💰 PNL REAL
        profit_usd = (entry - price) * qty

        if DEBUG:
            print(f"{symbol} | Price: {price} | Entry: {entry} | PNL: ${profit_usd:.2f}")

        # 🚫 AÚN NO ACTIVA TRAILING
        if profit_usd < TRAIL_TRIGGER_USD:
            return

        # 🔥 TRAILING 75%
        lock_profit = profit_usd * TRAIL_LOCK_PERCENT

        # convertir USD → precio
        new_sl = entry - (lock_profit / qty)
        new_sl = adjust_price(new_sl, tick)

        print(f"🔄 {symbol} | New SL calculado: {new_sl}")

       # ✅ ACTUALIZAR SL
        if update_stop_loss(symbol, new_sl, qty):
            data["sl"] = new_sl
            
        return
 
        # 🧹 CANCELAR SL ANTERIOR
        if not cancel_sl_orders(symbol):
            return

        time.sleep(0.2)

        # ✅ NUEVO SL
        client.futures_create_order(
            symbol=symbol,
            side="BUY",
            type="STOP_MARKET",
            stopPrice=new_sl,
            closePosition=True
        )

        data["sl"] = new_sl

        print(f"✅ TRAILING SL actualizado {symbol} -> {new_sl}")

    except Exception as e:
        print(f"⚠️ Trailing error {symbol}: {e}")

def cancel_all_sl_tp(symbol):
    try:
        orders = client.futures_get_open_orders(symbol=symbol)

        for o in orders:
            if o.get("closePosition") and o["type"] in ["STOP_MARKET", "TAKE_PROFIT_MARKET"]:
                try:
                    client.futures_cancel_order(
                        symbol=symbol,
                        orderId=o["orderId"]
                    )
                    print(f"🗑 Cancelado {symbol} {o['type']}")
                except:
                    pass

        # 🔥 esperar liberación REAL
        for _ in range(15):
            time.sleep(0.3)
            remaining = get_open_sl_orders(symbol)
            if not remaining:
                return True

        print(f"⚠️ Binance no liberó órdenes {symbol}")
        return False

    except Exception as e:
        print(f"Cancel error {symbol}: {e}")
        return False
    
def get_open_sl_orders(symbol):
    try:
        orders = client.futures_get_open_orders(symbol=symbol)
        return [
            o for o in orders
            if o.get("closePosition") and o["type"] in ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
        ]
    except:
        return []

# ========= MONITOR =========
def monitor():
    while True:
        if DEBUG:
            print(f"\nMonitor {datetime.datetime.now()} - Active orders: {len(active_orders)}")
        for symbol in list(active_orders.keys()):
            try:
                if not has_open_position(symbol):
                    send_telegram(f"🎯 Cerrada {symbol}")
                    del active_orders[symbol]
                    continue

                # print(f"Evaluando {symbol} - SL: {active_orders[symbol].get('sl')}")
                data = active_orders[symbol]
                update_trailing_sl(symbol, data)

            except Exception as e:
                print(f"Monitor error {symbol}: {e}")

        time.sleep(60)

def load_active_positions():
    print("🔄 Cargando posiciones desde Binance...")

    try:
        positions = client.futures_position_information()
        open_orders = client.futures_get_open_orders()

        for p in positions:
            symbol = p["symbol"]
            qty = float(p["positionAmt"])

            # Solo posiciones abiertas
            if qty == 0:
                continue

            entry = float(p["entryPrice"])
            qty = abs(qty)

            # Buscar SL existente
            sl_price = None
            for o in open_orders:
                if o["symbol"] == symbol and o.get("closePosition"):
                    if o["type"] == "STOP_MARKET":
                        sl_price = float(o["stopPrice"])

                        if sl_price is None:
                            print(f"⚠️ {symbol} sin SL, creando uno nuevo")
                            sl_price = set_stop_loss(symbol, entry, qty)

            active_orders[symbol] = {
                "entry": entry,
                "qty": qty,
                "sl": sl_price
            }

            print(f"✅ Recuperado {symbol} | Entry: {entry} | SL: {sl_price}")

    except Exception as e:
        print(f"❌ Error cargando posiciones: {e}")
        

# def cancel_sl_orders(symbol):
#     try:
#         orders = get_open_sl_orders(symbol)

#         for o in orders:
#             try:
#                 client.futures_cancel_order(symbol=symbol, orderId=o["orderId"])
#             except:
#                 pass

#         # 🔥 esperar a que Binance libere
#         for _ in range(10):
#             time.sleep(0.25)
#             if not get_open_sl_orders(symbol):
#                 return True

#         print(f"⚠️ No se liberaron SLs en {symbol}")
#         return False

#     except Exception as e:
#         print(f"Cancel error {symbol}: {e}")
#         return False

def place_stop_loss_safe(symbol, stop_price):
    try:
        if get_open_sl_orders(symbol):
            return False

        client.futures_create_order(
            symbol=symbol,
            side="BUY",
            type="STOP_MARKET",
            stopPrice=stop_price,
            closePosition=True
        )
        return True

    except Exception as e:
        print(f"SL create error {symbol}: {e}")
        return False
        
import math

def adjust_qty(qty, step):
    if step == 0:
        return qty

    precision = int(round(-math.log(step, 10), 0))
    qty = math.floor(qty / step) * step
    return round(qty, precision)

def adjust_price(price, tick):
    if tick == 0:
        return price

    precision = int(round(-math.log(tick, 10), 0))
    price = math.floor(price / tick) * tick
    return round(price, precision)

# ========= MAIN =========
def run():
    print("+-----CONFIGURACION DE PATRON: {}V verdes diario + {}V rojas 4H + {} velas bajistas 15m-----+".format(DAILY_GREEN_CANDLES, H4_RED_CANDLES, M15_LOOKBACK))
    symbols = get_symbols()
    #symbols = filter_symbols_by_volume(symbols)
    #symbols = [s for s in symbols if s in {"APRUSDT"}]  # Filtrar blacklist
    
    # 👇 NUEVO
    load_active_positions()

    threading.Thread(target=monitor, daemon=True).start()

    while True:
        print(f"\nScan {datetime.datetime.now()}")

        for symbol in symbols:
            try:
                # if CHECK_POSITION and not TEST_MODE:
                #     if has_open_position(symbol):
                #         continue

                if detect_signal(symbol):
                    send_telegram(
                        f"📉 SIGNAL PRO\n{symbol}\n"
                        f"✔ 3D verdes\n✔ 2x 4H rojas\n✔ Pump\n✔ 15m bajista"
                    )

                    entry, qty = open_short(symbol)

                    send_telegram(f"SHORT {symbol}\nEntry: {entry}\nQty: {qty}")

                    clean_orders(symbol)
                    set_stop_loss(symbol, entry, qty)

                    active_orders[symbol] = {
                        "entry": entry,
                        "qty": qty,
                        "sl": None
                    }

            except Exception as e:
                print(f"Error {symbol}: {e}")

        time.sleep(300)

# ========= START =========
def start():
    while True:
        try:
            run()
        except Exception as e:
            print(f"Crash: {e}")
            traceback.print_exc()
            time.sleep(60)

if __name__ == "__main__":
    start()