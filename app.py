
import os
import json
import time
import hmac
import threading
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from flask import (
    Flask, request, redirect, url_for, session,
    jsonify, render_template_string
)
from pybit.unified_trading import HTTP

# ==========================================================
# COINSENTRA TRADING BRAIN V7.4 - RENDER EDITION
# Private password-protected dashboard
# PAPER TRADING ONLY - NO REAL ORDERS
# ==========================================================

VERSION = "7.4"

# ---------------- SETTINGS ----------------

SYMBOLS = ["ETHUSDT", "XRPUSDT", "SOLUSDT", "SUIUSDT"]

STARTING_BALANCE = 100.0
MAX_LEVERAGE = 6
MAX_OPEN_POSITIONS = 2

TAKE_PROFIT_PERCENT = 3.0
STOP_LOSS_PERCENT = 2.0

RISK_PER_TRADE = 0.005
MAX_MARGIN_PERCENT = 0.10
DAILY_LOSS_LIMIT = 6.0

REFRESH_SECONDS = 30
FEE_RATE = 0.00055

CATEGORY = "linear"
TESTNET = False

DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)

STATE_FILE = os.path.join(DATA_DIR, "coinsentra_state.json")
JOURNAL_FILE = os.path.join(DATA_DIR, "coinsentra_trades.json")

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY") or os.urandom(32).hex()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=86400
)

data_lock = threading.RLock()
bot_thread = None
bot_thread_lock = threading.Lock()

# Public market data only. No API keys are used.
session_bybit = HTTP(testnet=TESTNET)

# ---------------- STORAGE ----------------

def load_json(filename, default):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(filename, data):
    temp_file = filename + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_file, filename)
    except Exception:
        logging.exception("Could not save %s", filename)


state = load_json(STATE_FILE, {
    "balance": STARTING_BALANCE,
    "realized_pnl": 0.0,
    "daily_pnl": 0.0,
    "daily_date": datetime.now().strftime("%Y-%m-%d"),
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "positions": {},
    "learning_factor": 1.0,
    "last_update": None,
    "bot_status": "Starting"
})

journal = load_json(JOURNAL_FILE, [])

state.setdefault("balance", STARTING_BALANCE)
state.setdefault("realized_pnl", 0.0)
state.setdefault("daily_pnl", 0.0)
state.setdefault("daily_date", datetime.now().strftime("%Y-%m-%d"))
state.setdefault("total_trades", 0)
state.setdefault("wins", 0)
state.setdefault("losses", 0)
state.setdefault("positions", {})
state.setdefault("learning_factor", 1.0)
state.setdefault("last_update", None)
state.setdefault("bot_status", "Starting")

latest_analyses = []
latest_prices = {}
last_error = None


def save_all():
    with data_lock:
        save_json(STATE_FILE, state)
        save_json(JOURNAL_FILE, journal)


def reset_daily_if_needed():
    today = datetime.now().strftime("%Y-%m-%d")

    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_pnl"] = 0.0
        save_all()


def money(value):
    return round(float(value), 4)


def api_result(response):
    if response.get("retCode") != 0:
        raise Exception(response.get("retMsg", "Bybit API error"))
    return response.get("result", {})


# ---------------- MARKET DATA ----------------

def get_candles(symbol, interval="5", limit=100):
    response = session_bybit.get_kline(
        category=CATEGORY,
        symbol=symbol,
        interval=interval,
        limit=limit
    )

    rows = api_result(response).get("list", [])
    rows.reverse()

    return [
        {
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5])
        }
        for row in rows
    ]


def get_price(symbol):
    response = session_bybit.get_tickers(
        category=CATEGORY,
        symbol=symbol
    )
    rows = api_result(response).get("list", [])

    if not rows:
        return None

    return float(rows[0]["lastPrice"])


def get_orderbook(symbol):
    try:
        response = session_bybit.get_orderbook(
            category=CATEGORY,
            symbol=symbol,
            limit=25
        )

        result = api_result(response)
        bids = result.get("b", [])
        asks = result.get("a", [])

        bid_volume = sum(float(x[1]) for x in bids)
        ask_volume = sum(float(x[1]) for x in asks)
        total = bid_volume + ask_volume

        imbalance = (
            (bid_volume - ask_volume) / total * 100
            if total else 0.0
        )

        best_bid = float(bids[0][0]) if bids else 0
        best_ask = float(asks[0][0]) if asks else 0

        if best_bid and best_ask:
            mid = (best_bid + best_ask) / 2
            spread = (best_ask - best_bid) / mid * 100
        else:
            spread = 0.0

        return imbalance, spread

    except Exception:
        return 0.0, 0.0


def get_funding(symbol):
    try:
        response = session_bybit.get_tickers(
            category=CATEGORY,
            symbol=symbol
        )
        rows = api_result(response).get("list", [])
        if rows:
            return float(rows[0].get("fundingRate", 0) or 0)
    except Exception:
        pass

    return 0.0


def get_open_interest(symbol):
    try:
        response = session_bybit.get_open_interest(
            category=CATEGORY,
            symbol=symbol,
            intervalTime="5min",
            limit=2
        )

        rows = api_result(response).get("list", [])

        if len(rows) >= 2:
            old_value = float(rows[1]["openInterest"])
            new_value = float(rows[0]["openInterest"])

            if old_value:
                return (new_value - old_value) / old_value * 100

    except Exception:
        pass

    return 0.0


# ---------------- INDICATORS ----------------

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (value - result) * multiplier + result

    return result


def calculate_rsi(values, period=14):
    if len(values) < period + 1:
        return 50.0

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    recent = changes[-period:]
    gains = [x for x in recent if x > 0]
    losses = [-x for x in recent if x < 0]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_bollinger(values, period=20):
    recent = values[-period:]

    if len(recent) < period:
        return None, None

    average = sum(recent) / period
    variance = sum((x - average) ** 2 for x in recent) / period
    deviation = variance ** 0.5

    return average + 2 * deviation, average - 2 * deviation


def calculate_vwap(candles):
    recent = candles[-30:]
    numerator = 0.0
    denominator = 0.0

    for candle in recent:
        typical = (
            candle["high"] + candle["low"] + candle["close"]
        ) / 3

        volume = candle["volume"]
        numerator += typical * volume
        denominator += volume

    return numerator / denominator if denominator else None


def calculate_relative_volume(candles):
    if len(candles) < 21:
        return 1.0

    previous = [x["volume"] for x in candles[-21:-1]]
    average = sum(previous) / len(previous)

    return candles[-1]["volume"] / average if average else 1.0


def calculate_macd(values):
    if len(values) < 35:
        return 0.0, 0.0

    macd_series = []

    for i in range(26, len(values)):
        part = values[:i + 1]
        fast = ema(part, 12)
        slow = ema(part, 26)

        if fast is not None and slow is not None:
            macd_series.append(fast - slow)

    if not macd_series:
        return 0.0, 0.0

    macd_value = macd_series[-1]
    signal = ema(macd_series, 9) if len(macd_series) >= 9 else 0.0

    return macd_value, signal or 0.0


# ---------------- ANALYSIS ----------------

def analyze_symbol(symbol):
    try:
        candles = get_candles(symbol, "5", 100)
        candles_15m = get_candles(symbol, "15", 70)

        if len(candles) < 60 or len(candles_15m) < 30:
            return None

        closes = [x["close"] for x in candles]
        closes_15m = [x["close"] for x in candles_15m]

        price = closes[-1]

        ema9 = ema(closes, 9)
        ema21 = ema(closes, 21)
        ema50 = ema(closes, 50)

        rsi = calculate_rsi(closes)
        macd, macd_signal = calculate_macd(closes)

        upper, lower = calculate_bollinger(closes)
        vwap = calculate_vwap(candles)
        relative_volume = calculate_relative_volume(candles)

        ema9_15 = ema(closes_15m, 9)
        ema21_15 = ema(closes_15m, 21)

        imbalance, spread = get_orderbook(symbol)
        funding = get_funding(symbol)
        oi_change = get_open_interest(symbol)

        score = 50

        score += 8 if ema9 > ema21 else -8
        score += 8 if ema21 > ema50 else -8
        score += 8 if macd > macd_signal else -8

        if 50 <= rsi <= 68:
            score += 8
        elif rsi > 75:
            score -= 10
        elif rsi < 30:
            score += 3
        elif rsi < 45:
            score -= 5

        if vwap:
            score += 5 if price > vwap else -5

        if relative_volume >= 1.5:
            score += 8
        elif relative_volume < 0.7:
            score -= 5

        if imbalance > 10:
            score += 8
        elif imbalance < -10:
            score -= 8

        if spread > 0.15:
            score -= 8

        if oi_change > 0.5:
            score += 3
        elif oi_change < -0.5:
            score -= 3

        if funding > 0.001:
            score -= 4
        elif funding < -0.001:
            score += 4

        score = max(0, min(100, score))

        confirm_long = ema9_15 > ema21_15
        confirm_short = ema9_15 < ema21_15

        signal = "WAIT"

        if score >= 68 and confirm_long:
            signal = "LONG"
        elif score <= 32 and confirm_short:
            signal = "SHORT"

        if upper and lower:
            if price > upper and rsi > 75:
                signal = "WAIT"
            elif price < lower and rsi < 25:
                signal = "WAIT"

        return {
            "symbol": symbol,
            "price": price,
            "score": score,
            "signal": signal,
            "rsi": rsi,
            "relative_volume": relative_volume,
            "order_imbalance": imbalance,
            "spread": spread,
            "funding": funding,
            "oi_change": oi_change,
            "confirm_long": confirm_long,
            "confirm_short": confirm_short
        }

    except Exception:
        logging.exception("Analysis failed for %s", symbol)
        return None


# ---------------- LEARNING / SIZING ----------------

def update_learning():
    recent = journal[-100:]

    if len(recent) < 10:
        state["learning_factor"] = 1.0
        return

    wins = sum(
        1 for trade in recent
        if float(trade.get("net_pnl", 0)) > 0
    )

    win_rate = wins / len(recent)

    if win_rate >= 0.60:
        state["learning_factor"] = 1.10
    elif win_rate >= 0.50:
        state["learning_factor"] = 1.0
    elif win_rate >= 0.40:
        state["learning_factor"] = 0.85
    else:
        state["learning_factor"] = 0.70


def calculate_position_size(entry):
    balance = float(state["balance"])

    risk_amount = (
        balance * RISK_PER_TRADE *
        float(state.get("learning_factor", 1.0))
    )

    stop_distance = entry * (STOP_LOSS_PERCENT / 100)

    if stop_distance <= 0:
        return 0.0, 0.0

    quantity = risk_amount / stop_distance

    max_margin = balance * MAX_MARGIN_PERCENT
    max_notional = max_margin * MAX_LEVERAGE

    quantity = min(quantity, max_notional / entry)

    quantity = float(
        Decimal(str(quantity)).quantize(
            Decimal("0.000001"),
            rounding=ROUND_DOWN
        )
    )

    margin = quantity * entry / MAX_LEVERAGE

    return quantity, margin


# ---------------- PAPER TRADE MANAGEMENT ----------------

def calculate_trade_pnl(position, current_price):
    entry = float(position["entry"])
    quantity = float(position["quantity"])

    if position["side"] == "LONG":
        gross = (current_price - entry) * quantity
    else:
        gross = (entry - current_price) * quantity

    entry_fee = entry * quantity * FEE_RATE
    exit_fee = current_price * quantity * FEE_RATE

    total_fees = entry_fee + exit_fee

    return {
        "gross": gross,
        "fees": total_fees,
        "net": gross - total_fees
    }


def open_position(analysis):
    symbol = analysis["symbol"]
    direction = analysis["signal"]
    entry = analysis["price"]

    with data_lock:
        if direction not in ("LONG", "SHORT"):
            return

        if symbol in state["positions"]:
            return

        if len(state["positions"]) >= MAX_OPEN_POSITIONS:
            return

        # Daily limit applies to realized losses.
        if float(state["daily_pnl"]) <= -DAILY_LOSS_LIMIT:
            return

        quantity, margin = calculate_position_size(entry)

        if quantity <= 0:
            return

        if direction == "LONG":
            tp = entry * (1 + TAKE_PROFIT_PERCENT / 100)
            sl = entry * (1 - STOP_LOSS_PERCENT / 100)
        else:
            tp = entry * (1 - TAKE_PROFIT_PERCENT / 100)
            sl = entry * (1 + STOP_LOSS_PERCENT / 100)

        trade_number = (
            int(state["total_trades"]) +
            len(state["positions"]) + 1
        )

        position = {
            "trade_id": "T" + str(int(time.time() * 1000)),
            "trade_number": trade_number,
            "symbol": symbol,
            "side": direction,
            "entry": entry,
            "quantity": quantity,
            "margin": margin,
            "leverage": MAX_LEVERAGE,
            "take_profit": tp,
            "stop_loss": sl,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "entry_score": analysis["score"]
        }

        state["positions"][symbol] = position
        logging.info("Opened paper trade %s %s", symbol, direction)
        save_all()


def close_position(symbol, exit_price, reason):
    with data_lock:
        position = state["positions"].get(symbol)

        if not position:
            return

        pnl = calculate_trade_pnl(position, exit_price)
        net_pnl = pnl["net"]

        trade = {
            "trade_id": position["trade_id"],
            "trade_number": position["trade_number"],
            "symbol": symbol,
            "side": position["side"],
            "entry": position["entry"],
            "exit": exit_price,
            "quantity": position["quantity"],
            "leverage": position["leverage"],
            "gross_pnl": pnl["gross"],
            "fees": pnl["fees"],
            "net_pnl": net_pnl,
            "reason": reason,
            "opened_at": position["opened_at"],
            "closed_at": datetime.now(timezone.utc).isoformat()
        }

        state["balance"] += net_pnl
        state["realized_pnl"] += net_pnl
        state["daily_pnl"] += net_pnl
        state["total_trades"] += 1

        if net_pnl > 0:
            state["wins"] += 1
        else:
            state["losses"] += 1

        journal.append(trade)
        del state["positions"][symbol]

        update_learning()
        save_all()

        logging.info(
            "Closed %s: net PNL %.4f (%s)",
            symbol, net_pnl, reason
        )


def manage_positions():
    with data_lock:
        symbols = list(state["positions"].keys())

    for symbol in symbols:
        try:
            price = get_price(symbol)

            if price is None:
                continue

            with data_lock:
                latest_prices[symbol] = price
                position = state["positions"].get(symbol)

            if not position:
                continue

            if position["side"] == "LONG":
                if price >= position["take_profit"]:
                    close_position(symbol, price, "TAKE PROFIT")
                elif price <= position["stop_loss"]:
                    close_position(symbol, price, "STOP LOSS")

            else:
                if price <= position["take_profit"]:
                    close_position(symbol, price, "TAKE PROFIT")
                elif price >= position["stop_loss"]:
                    close_position(symbol, price, "STOP LOSS")

        except Exception:
            logging.exception("Position management failed: %s", symbol)


# ---------------- BOT LOOP ----------------

def bot_loop():
    global latest_analyses, last_error

    logging.info("CoinSentra Trading Brain V%s started", VERSION)

    while True:
        try:
            reset_daily_if_needed()
            manage_positions()

            analyses = []

            for symbol in SYMBOLS:
                result = analyze_symbol(symbol)

                if result:
                    analyses.append(result)
                    with data_lock:
                        latest_prices[symbol] = result["price"]

            for analysis in analyses:
                with data_lock:
                    open_count = len(state["positions"])

                if open_count >= MAX_OPEN_POSITIONS:
                    break

                if analysis["signal"] in ("LONG", "SHORT"):
                    open_position(analysis)

            with data_lock:
                latest_analyses = analyses
                state["last_update"] = datetime.now(
                    timezone.utc
                ).isoformat()
                state["bot_status"] = "Running"
                last_error = None
                update_learning()
                save_all()

        except Exception as exc:
            last_error = str(exc)
            logging.exception("Bot loop error")

            with data_lock:
                state["bot_status"] = "Market data error"

        time.sleep(REFRESH_SECONDS)


def start_bot():
    global bot_thread

    with bot_thread_lock:
        if bot_thread is not None and bot_thread.is_alive():
            return

        bot_thread = threading.Thread(
            target=bot_loop,
            daemon=True,
            name="CoinSentraTradingBot"
        )
        bot_thread.start()


# ---------------- PASSWORD PROTECTION ----------------

LOGIN_HTML = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CoinSentra Login</title>
<style>
body{margin:0;background:#08111f;color:#e8f0ff;font-family:Arial;
display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#111e30;padding:30px;border-radius:18px;width:85%;max-width:360px}
h2{color:#38d9a9}
input,button{box-sizing:border-box;width:100%;padding:14px;margin-top:12px;
border-radius:9px;border:0;font-size:16px}
input{background:#e8f0ff;color:#101827}
button{background:#16b98c;color:#06131c;font-weight:bold}
.error{color:#ff7777}
.small{color:#9eafc6;font-size:13px}
</style>
</head>
<body>
<div class="box">
<h2>COINSENTRA AI</h2>
<h3>Trading Brain V7.4</h3>
<p class="small">Private dashboard login</p>
{% if error %}<p class="error">{{error}}</p>{% endif %}
<form method="post">
<input type="password" name="password" placeholder="Dashboard password" required>
<button type="submit">LOGIN</button>
</form>
<p class="small">Paper trading only. No real orders.</p>
</div>
</body>
</html>
"""


DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CoinSentra Trading Brain</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#08111f;color:#e7eefb;font-family:Arial,sans-serif}
header{padding:20px;background:#101d30;border-bottom:1px solid #263a54}
h1{margin:0;color:#38d9a9;font-size:23px}
.sub{color:#9eb0c9;font-size:13px;margin-top:6px}
main{max-width:1100px;margin:auto;padding:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}
.card{background:#122138;border:1px solid #263a54;border-radius:13px;padding:14px;margin-bottom:10px}
.label{color:#9eb0c9;font-size:12px}
.value{font-size:21px;font-weight:bold;margin-top:7px;overflow-wrap:anywhere}
h2{font-size:17px;color:#38d9a9;margin:24px 0 12px}
h3{font-size:16px;margin:5px 0 12px}
.trade{border-left:4px solid #38d9a9}
.closed{border-left:4px solid #7395c4}
.row{display:flex;justify-content:space-between;gap:12px;padding:5px 0;
border-bottom:1px solid #26364c;font-size:13px}
.row span:first-child{color:#9eb0c9}
.good{color:#38d9a9}.bad{color:#ff7777}.neutral{color:#e7eefb}
.pill{display:inline-block;padding:5px 9px;border-radius:20px;
background:#243b56;font-size:12px}
button{border:0;border-radius:8px;padding:10px 14px;
background:#16b98c;color:#06131c;font-weight:bold}
a{color:#38d9a9;text-decoration:none}
footer{color:#8294ad;text-align:center;padding:25px;font-size:12px}
@media(max-width:480px){.value{font-size:18px}.row{font-size:12px}}
</style>
</head>
<body>
<header>
<h1>COINSENTRA TRADING BRAIN</h1>
<div class="sub">V7.4 · Private Render Dashboard · PAPER TRADING</div>
<div class="sub" id="status">Connecting...</div>
</header>
<main>
<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin:15px 0">
<span class="pill">ETH · XRP · SOL · SUI</span>
<form action="/logout" method="post"><button type="submit">Logout</button></form>
</div>

<h2>ACCOUNT SUMMARY</h2>
<div class="grid" id="summary"></div>

<h2>OPEN TRADES — EACH TRADE SEPARATELY</h2>
<div id="openTrades"></div>

<h2>CLOSED TRADES — INDIVIDUAL HISTORY</h2>
<div id="closedTrades"></div>

<h2>MARKET SCANNER</h2>
<div id="scanner"></div>

<div class="card">
<h3>Trading settings</h3>
<div class="row"><span>Entry timeframe</span><b>5 minutes</b></div>
<div class="row"><span>Confirmation timeframe</span><b>15 minutes</b></div>
<div class="row"><span>Take profit</span><b>3%</b></div>
<div class="row"><span>Stop loss</span><b>2%</b></div>
<div class="row"><span>Maximum leverage</span><b>6x</b></div>
<div class="row"><span>Maximum open trades</span><b>2</b></div>
<div class="row"><span>Daily realized loss limit</span><b>$6</b></div>
<div class="row"><span>Refresh interval</span><b>30 seconds</b></div>
</div>
</main>
<footer>CoinSentra Trading Brain · Simulated results only · Not financial advice</footer>

<script>
function fmt(v){return "$"+Number(v||0).toFixed(4)}
function cls(v){return Number(v)>=0?"good":"bad"}
function row(label,value){return `<div class="row"><span>${label}</span><b>${value}</b></div>`}
function card(label,value){return `<div class="card"><div class="label">${label}</div><div class="value">${value}</div></div>`}

async function refresh(){
 try{
  const response=await fetch("/api/data",{cache:"no-store"});
  if(response.status===401){location.href="/login";return}
  const d=await response.json();

  document.getElementById("status").textContent=
   "Bot: "+d.bot_status+" · Last update: "+(d.last_update||"Waiting for first scan");

  const s=d.summary;
  document.getElementById("summary").innerHTML=
   card("Simulated balance",fmt(s.balance))+
   card("Realized lifetime PNL",`<span class="${cls(s.realized_pnl)}">${fmt(s.realized_pnl)}</span>`)+
   card("Today's realized PNL",`<span class="${cls(s.daily_pnl)}">${fmt(s.daily_pnl)}</span>`)+
   card("Open net PNL",`<span class="${cls(s.unrealized_net)}">${fmt(s.unrealized_net)}</span>`)+
   card("Total PNL",`<span class="${cls(s.total_pnl)}">${fmt(s.total_pnl)}</span>`)+
   card("Estimated equity",fmt(s.equity))+
   card("Win rate",s.win_rate.toFixed(1)+"%")+
   card("Closed trades",s.total_trades+" · "+s.wins+" wins / "+s.losses+" losses");

  const open=document.getElementById("openTrades");
  if(!d.open_trades.length){
   open.innerHTML='<div class="card">No open paper trades.</div>';
  }else{
   open.innerHTML=d.open_trades.map((t,i)=>`
    <div class="card trade">
     <h3>TRADE ${i+1} · ${t.trade_id}</h3>
     ${row("Symbol",t.symbol)}
     ${row("Direction",t.side)}
     ${row("Entry price",fmt(t.entry))}
     ${row("Current price",fmt(t.current_price))}
     ${row("Gross unrealized PNL",`<span class="${cls(t.gross_pnl)}">${fmt(t.gross_pnl)}</span>`)}
     ${row("Estimated fees",fmt(t.fees))}
     ${row("NET unrealized PNL",`<span class="${cls(t.net_pnl)}">${fmt(t.net_pnl)}</span>`)}
     ${row("Take profit",fmt(t.take_profit))}
     ${row("Stop loss",fmt(t.stop_loss))}
     ${row("Quantity",t.quantity)}
     ${row("Margin",fmt(t.margin))}
     ${row("Leverage",t.leverage+"x")}
     ${row("Entry score",t.entry_score)}
     ${row("Opened",t.opened_at)}
    </div>`).join("");
  }

  const closed=document.getElementById("closedTrades");
  if(!d.closed_trades.length){
   closed.innerHTML='<div class="card">No closed trades yet.</div>';
  }else{
   closed.innerHTML=d.closed_trades.map((t,i)=>`
    <div class="card closed">
     <h3>CLOSED TRADE ${i+1} · ${t.trade_id}</h3>
     ${row("Symbol",t.symbol)}
     ${row("Direction",t.side)}
     ${row("Entry",fmt(t.entry))}
     ${row("Exit",fmt(t.exit))}
     ${row("Gross PNL",fmt(t.gross_pnl))}
     ${row("Fees",fmt(t.fees))}
     ${row("NET RESULT",`<span class="${cls(t.net_pnl)}">${fmt(t.net_pnl)}</span>`)}
     ${row("Exit reason",t.reason)}
     ${row("Closed",t.closed_at)}
    </div>`).join("");
  }

  const scanner=document.getElementById("scanner");
  scanner.innerHTML=d.scanner.map(x=>`
   <div class="card">
    <h3>${x.symbol} <span class="pill">${x.signal}</span></h3>
    ${row("Price",fmt(x.price))}
    ${row("AI score",x.score+"/100")}
    ${row("RSI",Number(x.rsi).toFixed(2))}
    ${row("Relative volume",Number(x.relative_volume).toFixed(2)+"x")}
    ${row("Order book imbalance",Number(x.order_imbalance).toFixed(2)+"%")}
    ${row("Spread",Number(x.spread).toFixed(4)+"%")}
    ${row("Funding rate",x.funding)}
    ${row("Open interest change",Number(x.oi_change).toFixed(3)+"%")}
    ${row("15m confirmation",x.confirmation)}
   </div>`).join("") || '<div class="card">Waiting for market data...</div>';

 }catch(e){
  document.getElementById("status").textContent="Dashboard connection error";
 }
}
refresh();
setInterval(refresh,10000);
</script>
</body>
</html>
"""


# ---------------- WEB AUTHENTICATION ----------------

@app.before_request
def require_login():
    allowed = {"login", "health"}

    if request.endpoint in allowed:
        return None

    if not session.get("authenticated"):
        return redirect(url_for("login"))

    return None


@app.route("/health")
def health():
    return jsonify({"status": "ok", "version": VERSION})


@app.route("/login", methods=["GET", "POST"])
def login():
    if not DASHBOARD_PASSWORD:
        return (
            "Dashboard password is not configured. "
            "Set DASHBOARD_PASSWORD in Render environment variables.",
            503
        )

    error = None

    if request.method == "POST":
        supplied = request.form.get("password", "")

        if hmac.compare_digest(supplied, DASHBOARD_PASSWORD):
            session.clear()
            session.permanent = True
            session["authenticated"] = True
            return redirect(url_for("dashboard"))

        error = "Incorrect password."

    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/data")
def api_data():
    with data_lock:
        positions = list(state["positions"].values())
        trades = list(journal[-20:])
        analyses = list(latest_analyses)

        summary = {
            "balance": float(state["balance"]),
            "realized_pnl": float(state["realized_pnl"]),
            "daily_pnl": float(state["daily_pnl"]),
            "total_trades": int(state["total_trades"]),
            "wins": int(state["wins"]),
            "losses": int(state["losses"]),
            "learning_factor": float(state["learning_factor"]),
            "last_update": state.get("last_update"),
            "bot_status": state.get("bot_status", "Starting")
        }

        prices = dict(latest_prices)

    unrealized_gross = 0.0
    unrealized_net = 0.0
    open_trades = []

    for position in positions:
        symbol = position["symbol"]
        current = prices.get(symbol, position["entry"])

        pnl = calculate_trade_pnl(position, current)
        unrealized_gross += pnl["gross"]
        unrealized_net += pnl["net"]

        open_trades.append({
            **position,
            "current_price": current,
            "gross_pnl": pnl["gross"],
            "fees": pnl["fees"],
            "net_pnl": pnl["net"]
        })

    total_pnl = summary["realized_pnl"] + unrealized_net
    equity = summary["balance"] + unrealized_net

    summary["unrealized_gross"] = unrealized_gross
    summary["unrealized_net"] = unrealized_net
    summary["total_pnl"] = total_pnl
    summary["equity"] = equity
    summary["win_rate"] = (
        summary["wins"] / summary["total_trades"] * 100
        if summary["total_trades"] else 0.0
    )

    scanner = []

    for item in analyses:
        scanner.append({
            "symbol": item["symbol"],
            "price": item["price"],
            "score": item["score"],
            "signal": item["signal"],
            "rsi": item["rsi"],
            "relative_volume": item["relative_volume"],
            "order_imbalance": item["order_imbalance"],
            "spread": item["spread"],
            "funding": item["funding"],
            "oi_change": item["oi_change"],
            "confirmation": (
                "LONG" if item["confirm_long"]
                else "SHORT" if item["confirm_short"]
                else "NO"
            )
        })

    return jsonify({
        "summary": summary,
        "open_trades": open_trades,
        "closed_trades": list(reversed(trades)),
        "scanner": scanner,
        "bot_status": summary["bot_status"],
        "last_update": summary["last_update"],
        "last_error": last_error
    })


# ---------------- START ----------------

start_bot()

if __name__ == "__main__":
    # Local testing only. Render uses Gunicorn.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
