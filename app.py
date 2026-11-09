
import os
import json
import time
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from pybit.unified_trading import HTTP

# ==========================================================
# COINSENTRA TRADING BRAIN V7.3
# Independent trade summaries + separate closed-trade history
# PAPER TRADING ONLY - NO REAL ORDERS
# ==========================================================

VERSION = "7.3"

# -------------------- SETTINGS -----------------------------

TESTNET = False
DRY_RUN = True

SYMBOLS = [
    "ETHUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "SUIUSDT"
]

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

STATE_FILE = "coinsentra_v7_state.json"
JOURNAL_FILE = "coinsentra_v7_trades.json"

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s"
)

# -------------------- BYBIT PUBLIC DATA --------------------

session = HTTP(testnet=TESTNET)

# -------------------- FILE STORAGE ------------------------

def load_json(filename, default):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(filename, data):
    try:
        with open(filename, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print("Save error:", e)


state = load_json(STATE_FILE, {
    "balance": STARTING_BALANCE,
    "realized_pnl": 0.0,
    "daily_pnl": 0.0,
    "daily_date": datetime.now().strftime("%Y-%m-%d"),
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "positions": {},
    "learning_factor": 1.0
})

journal = load_json(JOURNAL_FILE, [])

# Migrate older saved files safely.
state.setdefault("balance", STARTING_BALANCE)
state.setdefault("realized_pnl", 0.0)
state.setdefault("daily_pnl", 0.0)
state.setdefault("daily_date", datetime.now().strftime("%Y-%m-%d"))
state.setdefault("total_trades", 0)
state.setdefault("wins", 0)
state.setdefault("losses", 0)
state.setdefault("positions", {})
state.setdefault("learning_factor", 1.0)

# -------------------- HELPERS ------------------------------

def money(value):
    return f"${float(value):,.4f}"


def percentage(value):
    return f"{float(value):+.2f}%"


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def save_all():
    save_json(STATE_FILE, state)
    save_json(JOURNAL_FILE, journal)


def reset_daily_if_needed():
    today = datetime.now().strftime("%Y-%m-%d")

    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_pnl"] = 0.0
        save_all()


def api_result(response):
    if response.get("retCode") != 0:
        raise Exception(response.get("retMsg", "Bybit API error"))
    return response.get("result", {})


# -------------------- MARKET DATA --------------------------

def get_candles(symbol, interval="5", limit=100):
    response = session.get_kline(
        category=CATEGORY,
        symbol=symbol,
        interval=interval,
        limit=limit
    )

    result = api_result(response)
    rows = result.get("list", [])

    # Bybit returns newest candles first.
    rows.reverse()

    return [
        {
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5])
        }
        for row in rows
    ]


def get_price(symbol):
    response = session.get_tickers(
        category=CATEGORY,
        symbol=symbol
    )

    result = api_result(response)
    rows = result.get("list", [])

    if not rows:
        return None

    return float(rows[0]["lastPrice"])


def get_orderbook(symbol):
    try:
        response = session.get_orderbook(
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

        if total == 0:
            return 0.0, 0.0

        imbalance = (bid_volume - ask_volume) / total * 100

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
        response = session.get_tickers(
            category=CATEGORY,
            symbol=symbol
        )

        result = api_result(response)
        rows = result.get("list", [])

        if rows:
            return float(rows[0].get("fundingRate", 0) or 0)

    except Exception:
        pass

    return 0.0


def get_open_interest(symbol):
    try:
        response = session.get_open_interest(
            category=CATEGORY,
            symbol=symbol,
            intervalTime="5min",
            limit=2
        )

        result = api_result(response)
        rows = result.get("list", [])

        if len(rows) >= 2:
            old_value = float(rows[1]["openInterest"])
            new_value = float(rows[0]["openInterest"])

            if old_value:
                return (new_value - old_value) / old_value * 100

    except Exception:
        pass

    return 0.0


# -------------------- INDICATORS ---------------------------

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

    variance = sum(
        (x - average) ** 2 for x in recent
    ) / period

    deviation = variance ** 0.5

    return average + 2 * deviation, average - 2 * deviation


def calculate_vwap(candles):
    recent = candles[-30:]

    numerator = 0.0
    denominator = 0.0

    for candle in recent:
        typical = (
            candle["high"] +
            candle["low"] +
            candle["close"]
        ) / 3

        volume = candle["volume"]

        numerator += typical * volume
        denominator += volume

    if denominator == 0:
        return None

    return numerator / denominator


def calculate_relative_volume(candles):
    if len(candles) < 21:
        return 1.0

    previous = [
        x["volume"] for x in candles[-21:-1]
    ]

    average = sum(previous) / len(previous)

    if average == 0:
        return 1.0

    return candles[-1]["volume"] / average


def calculate_macd(values):
    if len(values) < 35:
        return 0.0, 0.0

    fast = ema(values, 12)
    slow = ema(values, 26)

    if fast is None or slow is None:
        return 0.0, 0.0

    macd_value = fast - slow

    # Approximate signal using the recent MACD values.
    macd_series = []

    for i in range(26, len(values)):
        part = values[:i + 1]
        macd_series.append(
            ema(part, 12) - ema(part, 26)
        )

    signal = ema(macd_series, 9) if len(macd_series) >= 9 else 0

    return macd_value, signal or 0.0


# -------------------- MARKET ANALYSIS ----------------------

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

        order_imbalance, spread = get_orderbook(symbol)
        funding = get_funding(symbol)
        oi_change = get_open_interest(symbol)

        score = 50

        if ema9 > ema21:
            score += 8
        else:
            score -= 8

        if ema21 > ema50:
            score += 8
        else:
            score -= 8

        if macd > macd_signal:
            score += 8
        else:
            score -= 8

        if 50 <= rsi <= 68:
            score += 8
        elif rsi > 75:
            score -= 10
        elif rsi < 30:
            score += 3
        elif rsi < 45:
            score -= 5

        if vwap and price > vwap:
            score += 5
        elif vwap:
            score -= 5

        if relative_volume >= 1.5:
            score += 8
        elif relative_volume < 0.7:
            score -= 5

        if order_imbalance > 10:
            score += 8
        elif order_imbalance < -10:
            score -= 8

        if spread > 0.15:
            score -= 8

        if oi_change > 0.5:
            score += 3
        elif oi_change < -0.5:
            score -= 3

        # Avoid extreme funding conditions.
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
            "ema9": ema9,
            "ema21": ema21,
            "ema50": ema50,
            "macd": macd,
            "macd_signal": macd_signal,
            "relative_volume": relative_volume,
            "order_imbalance": order_imbalance,
            "spread": spread,
            "funding": funding,
            "oi_change": oi_change,
            "confirm_long": confirm_long,
            "confirm_short": confirm_short
        }

    except Exception as e:
        logging.error("%s analysis error: %s", symbol, e)
        return None


# -------------------- LEARNING -----------------------------

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

    # This is a simple adaptive risk adjustment,
    # not machine-learning price prediction.
    if win_rate >= 0.60:
        factor = 1.10
    elif win_rate >= 0.50:
        factor = 1.0
    elif win_rate >= 0.40:
        factor = 0.85
    else:
        factor = 0.70

    state["learning_factor"] = factor


# -------------------- POSITION SIZING ----------------------

def calculate_position_size(entry, direction):
    balance = float(state["balance"])

    risk_amount = (
        balance *
        RISK_PER_TRADE *
        float(state.get("learning_factor", 1.0))
    )

    stop_distance = entry * (STOP_LOSS_PERCENT / 100)

    if stop_distance <= 0:
        return 0.0, 0.0

    quantity = risk_amount / stop_distance

    max_margin = balance * MAX_MARGIN_PERCENT
    max_notional = max_margin * MAX_LEVERAGE

    quantity = min(quantity, max_notional / entry)

    # Round down conservatively.
    quantity = float(
        Decimal(str(quantity)).quantize(
            Decimal("0.000001"),
            rounding=ROUND_DOWN
        )
    )

    margin = quantity * entry / MAX_LEVERAGE

    return quantity, margin


# -------------------- OPEN PAPER TRADE ---------------------

def open_position(analysis):
    symbol = analysis["symbol"]
    direction = analysis["signal"]
    entry = analysis["price"]

    if direction not in ("LONG", "SHORT"):
        return

    if symbol in state["positions"]:
        return

    if len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return

    if state["daily_pnl"] <= -DAILY_LOSS_LIMIT:
        return

    quantity, margin = calculate_position_size(entry, direction)

    if quantity <= 0:
        return

    if direction == "LONG":
        take_profit = entry * (1 + TAKE_PROFIT_PERCENT / 100)
        stop_loss = entry * (1 - STOP_LOSS_PERCENT / 100)
    else:
        take_profit = entry * (1 - TAKE_PROFIT_PERCENT / 100)
        stop_loss = entry * (1 + STOP_LOSS_PERCENT / 100)

    trade_number = state["total_trades"] + len(state["positions"]) + 1

    position = {
        "trade_id": f"T{int(time.time())}",
        "trade_number": trade_number,
        "symbol": symbol,
        "side": direction,
        "entry": entry,
        "quantity": quantity,
        "margin": margin,
        "leverage": MAX_LEVERAGE,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "entry_score": analysis["score"]
    }

    state["positions"][symbol] = position

    print(
        f"\nOPENED PAPER TRADE: {symbol} {direction} "
        f"at {money(entry)}"
    )

    save_all()


# -------------------- PNL CALCULATIONS ---------------------

def calculate_trade_pnl(position, current_price):
    entry = float(position["entry"])
    quantity = float(position["quantity"])

    if position["side"] == "LONG":
        gross = (current_price - entry) * quantity
    else:
        gross = (entry - current_price) * quantity

    entry_fee = entry * quantity * FEE_RATE
    exit_fee = current_price * quantity * FEE_RATE

    net = gross - entry_fee - exit_fee

    return {
        "gross": gross,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "total_fees": entry_fee + exit_fee,
        "net": net
    }


# -------------------- CLOSE PAPER TRADE --------------------

def close_position(symbol, exit_price, reason):
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
        "fees": pnl["total_fees"],
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

    print("\n" + "=" * 55)
    print("TRADE CLOSED")
    print("=" * 55)
    print("Trade ID:     ", trade["trade_id"])
    print("Symbol:       ", symbol)
    print("Direction:    ", trade["side"])
    print("Entry:        ", money(trade["entry"]))
    print("Exit:         ", money(trade["exit"]))
    print("Gross PNL:    ", money(trade["gross_pnl"]))
    print("Fees:         ", money(trade["fees"]))
    print("NET RESULT:   ", money(net_pnl))
    print("Reason:       ", reason)
    print("=" * 55)


# -------------------- MANAGE OPEN POSITIONS ----------------

def manage_positions():
    for symbol in list(state["positions"].keys()):
        try:
            position = state["positions"][symbol]
            price = get_price(symbol)

            if price is None:
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

        except Exception as e:
            logging.error("Position management %s: %s", symbol, e)


# -------------------- INDEPENDENT TRADE SUMMARY ------------

def display_open_trade(position, index):
    symbol = position["symbol"]

    try:
        current = get_price(symbol)
    except Exception:
        current = None

    print("\n" + "-" * 58)
    print(f"OPEN TRADE {index} | {position['trade_id']}")
    print("-" * 58)

    print("Symbol:           ", symbol)
    print("Direction:        ", position["side"])
    print("Entry price:      ", money(position["entry"]))

    if current is not None:
        pnl = calculate_trade_pnl(position, current)

        print("Current price:    ", money(current))
        print("Gross unrealized: ", money(pnl["gross"]))
        print("Estimated fees:   ", money(pnl["total_fees"]))
        print("NET unrealized:   ", money(pnl["net"]))

    else:
        print("Current price:     Not available")
        print("NET unrealized:    Not available")

    print("Take profit:      ", money(position["take_profit"]))
    print("Stop loss:        ", money(position["stop_loss"]))
    print("Quantity:         ", position["quantity"])
    print("Margin used:      ", money(position["margin"]))
    print("Leverage:         ", str(position["leverage"]) + "x")
    print("Entry score:      ", position.get("entry_score", "N/A"))
    print("Opened:           ", position["opened_at"])


def display_closed_trade(trade, index):
    print("\n" + "-" * 58)
    print(f"CLOSED TRADE {index} | {trade.get('trade_id', 'N/A')}")
    print("-" * 58)

    print("Symbol:           ", trade.get("symbol"))
    print("Direction:        ", trade.get("side"))
    print("Entry:            ", money(trade.get("entry", 0)))
    print("Exit:             ", money(trade.get("exit", 0)))
    print("Quantity:         ", trade.get("quantity", 0))
    print("Gross PNL:        ", money(trade.get("gross_pnl", 0)))
    print("Fees:             ", money(trade.get("fees", 0)))
    print("NET PNL:          ", money(trade.get("net_pnl", 0)))
    print("Exit reason:      ", trade.get("reason"))
    print("Closed:           ", trade.get("closed_at"))


# -------------------- FULL DASHBOARD -----------------------

def display_dashboard(analyses):
    clear_screen()
    reset_daily_if_needed()

    positions = list(state["positions"].values())

    unrealized_gross = 0.0
    unrealized_net = 0.0
    estimated_fees = 0.0

    for position in positions:
        try:
            current = get_price(position["symbol"])

            if current is not None:
                pnl = calculate_trade_pnl(position, current)
                unrealized_gross += pnl["gross"]
                unrealized_net += pnl["net"]
                estimated_fees += pnl["total_fees"]

        except Exception:
            pass

    realized = float(state["realized_pnl"])
    total_pnl = realized + unrealized_net
    equity = float(state["balance"]) + unrealized_net

    total_trades = int(state["total_trades"])
    wins = int(state["wins"])
    losses = int(state["losses"])

    win_rate = (
        wins / total_trades * 100
        if total_trades else 0.0
    )

    print("=" * 62)
    print("          COINSENTRA TRADING BRAIN V" + VERSION)
    print("             PAPER TRADING DASHBOARD")
    print("=" * 62)

    print("Updated:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("Mode: PAPER ONLY | Leverage:", MAX_LEVERAGE, "x")
    print("Symbols:", ", ".join(SYMBOLS))

    print("\n" + "=" * 62)
    print("ACCOUNT SUMMARY")
    print("=" * 62)

    print("Starting balance:       ", money(STARTING_BALANCE))
    print("Simulated balance:      ", money(state["balance"]))
    print("Realized lifetime PNL:  ", money(realized))
    print("Today's realized PNL:   ", money(state["daily_pnl"]))
    print("Open gross PNL:         ", money(unrealized_gross))
    print("Open estimated fees:    ", money(estimated_fees))
    print("Open NET PNL:           ", money(unrealized_net))
    print("TOTAL PNL:              ", money(total_pnl))
    print("Estimated account equity:", money(equity))

    print("\nClosed trades:", total_trades)
    print("Wins:", wins, "| Losses:", losses)
    print("Win rate:", percentage(win_rate))
    print("Learning factor:", state.get("learning_factor", 1.0))

    # Every open position is displayed independently.
    print("\n" + "=" * 62)
    print("OPEN TRADES - INDIVIDUAL RESULTS")
    print("=" * 62)

    if not positions:
        print("No open trades.")

    else:
        for index, position in enumerate(positions, start=1):
            display_open_trade(position, index)

    # Every closed trade is displayed independently.
    print("\n" + "=" * 62)
    print("CLOSED TRADES - INDIVIDUAL HISTORY")
    print("=" * 62)

    if not journal:
        print("No closed trades yet.")

    else:
        recent_trades = journal[-10:]

        for index, trade in enumerate(
            reversed(recent_trades),
            start=1
        ):
            display_closed_trade(trade, index)

    # Scanner is separate from the trade summaries.
    print("\n" + "=" * 62)
    print("MARKET SCANNER")
    print("=" * 62)

    if not analyses:
        print("No market data available.")

    for item in analyses:
        print("\n", item["symbol"])
        print(
            "Price:", money(item["price"]),
            "| Signal:", item["signal"],
            "| Score:", item["score"]
        )
        print(
            "RSI:", round(item["rsi"], 2),
            "| Relative volume:", round(item["relative_volume"], 2)
        )
        print(
            "Order book:", round(item["order_imbalance"], 2), "%",
            "| Spread:", round(item["spread"], 4), "%"
        )
        print(
            "Funding:", item["funding"],
            "| Open interest change:", round(item["oi_change"], 3), "%"
        )
        print(
            "15m confirmation:",
            "LONG" if item["confirm_long"]
            else "SHORT" if item["confirm_short"]
            else "NO"
        )

    print("\n" + "=" * 62)
    print("RISK SETTINGS")
    print("=" * 62)
    print("Take profit:", TAKE_PROFIT_PERCENT, "%")
    print("Stop loss:", STOP_LOSS_PERCENT, "%")
    print("Risk per trade:", RISK_PER_TRADE * 100, "%")
    print("Maximum open positions:", MAX_OPEN_POSITIONS)
    print("Daily loss limit:", money(DAILY_LOSS_LIMIT))
    print("Refresh:", REFRESH_SECONDS, "seconds")
    print("=" * 62)
    print("Paper trading only. No real orders are being placed.")
    print("Press CTRL+C to stop.")
    print("=" * 62)


# -------------------- MAIN LOOP ----------------------------

def main():
    print("Starting CoinSentra Trading Brain V" + VERSION)
    print("Paper trading only. Connecting to Bybit public data...")

    if not DRY_RUN:
        raise RuntimeError(
            "Safety lock: this version is paper-trading only."
        )

    while True:
        try:
            reset_daily_if_needed()

            # Manage existing trades first.
            manage_positions()

            # Analyze the market.
            analyses = []

            for symbol in SYMBOLS:
                result = analyze_symbol(symbol)

                if result:
                    analyses.append(result)

            # Open new trades if signals qualify.
            for analysis in analyses:
                if len(state["positions"]) >= MAX_OPEN_POSITIONS:
                    break

                if analysis["signal"] in ("LONG", "SHORT"):
                    open_position(analysis)

            update_learning()
            save_all()

            # Display independent open and closed trade summaries.
            display_dashboard(analyses)

            time.sleep(REFRESH_SECONDS)

        except KeyboardInterrupt:
            save_all()
            print("\nCoinSentra stopped. Your paper-trading data is saved.")
            break

        except Exception as e:
            logging.exception("Main loop error")
            print("\nMain loop error:", e)
            print("Retrying in 10 seconds...")
            time.sleep(10)


if __name__ == "__main__":
    main()
