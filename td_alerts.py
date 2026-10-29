#!/usr/bin/env python3
"""TD Sequential alerts -> Telegram, for indices and gold (MEXC perps) and BTC (Binance).

MEXC's index/gold perps track the cash index and spot gold like FTMO's CFDs and are real-time, but
trade 24/7; candles from hours the CFD market is closed (Friday 17:00 to Sunday 18:00 New York time,
and the daily 17:00-18:00 break) are dropped so the candles match an FTMO chart. If a feed refuses
(MEXC or Binance can block some countries, e.g. GitHub's US servers), the market's backup feed is
used: Yahoo futures (about 10 minutes late) or Coinbase.

Same rules as td_sequential.pine:
  - Setup 9: nine closes in a row below (buy) / above (sell) the close 4 candles earlier.
    "Perfected" when the 8th or 9th candle's low (high) takes out the lows (highs) of candles 6 and 7.
  - Countdown 13: after a 9, count closes at/below the low (buy) / at/above the high (sell) of 2
    candles earlier; the 13th must reach the 8th count's close. A new same-side 9 restarts it; an
    opposite 9 or a close beyond the setup's true range cancels it.
Alerts fire when the candle closes, like the indicator's alerts. The first run is silent; after
that, state.json remembers the last candle alerted per chart, so a late run catches up.

  python3 td_alerts.py --dry-run   print the latest signals on each chart, send nothing
  python3 td_alerts.py --once      one pass, then exit (GitHub Actions runs this every 15 minutes)
  python3 td_alerts.py             run forever, a pass after every 15-minute close (launchd)
Markets and timeframes live in config.json. The Telegram token and chat id come from the
TELEGRAM_TOKEN / TELEGRAM_CHAT_ID environment variables, or config.json's "telegram" block.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
STATE = os.path.join(HERE, "state.json")
SECS = {"15m": 900, "1h": 3600, "4h": 14400}
MEXC_IV = {"15m": "Min15", "1h": "Min60", "4h": "Hour4"}
YAHOO = {"15m": ("15m", "30d"), "1h": ("60m", "180d")}
NEW_YORK = ZoneInfo("America/New_York")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def get(url, headers=None):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read()[:200]!r}") from None
        except OSError:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


# ───────── Candles: (open time s, o, h, l, c), closed only, oldest first ─────────
def mexc_candles(symbol, tf):
    now = int(time.time())
    q = urllib.parse.urlencode({"interval": MEXC_IV[tf], "start": now - 2000 * SECS[tf], "end": now})
    k = get(f"https://contract.mexc.com/api/v1/contract/kline/{symbol}?{q}", {"User-Agent": "Mozilla/5.0"})["data"]
    return [(int(t), float(o), float(h), float(l), float(c))
            for t, o, h, l, c in zip(k["time"], k["open"], k["high"], k["low"], k["close"]) if t + SECS[tf] <= now]


def binance_candles(symbol, tf):
    rows = get(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={tf}&limit=1000")
    now = time.time()
    return [(r[0] // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows if r[0] / 1000 + SECS[tf] <= now]


def yahoo_candles(symbol, tf):
    iv, rng = YAHOO[tf]
    r = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval={iv}&range={rng}",
            {"User-Agent": "Mozilla/5.0"})["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    cutoff = time.time() - 11 * 60   # Yahoo's futures data runs about 10 minutes behind
    return [(t, o, h, l, c) for t, o, h, l, c in zip(r["timestamp"], q["open"], q["high"], q["low"], q["close"])
            if None not in (o, h, l, c) and t + SECS[tf] <= cutoff]


def coinbase_candles(product, tf, pages=4):
    secs, rows = SECS[tf], {}
    end = int(time.time()) // secs * secs
    iso = lambda t: dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%dT%H:%M:%SZ")
    for _ in range(pages):   # 300 candles per request
        start = end - 300 * secs
        q = urllib.parse.urlencode({"granularity": secs, "start": iso(start), "end": iso(end)})
        for t, lo, hi, op, cl, _ in get(f"https://api.exchange.coinbase.com/products/{product}/candles?{q}",
                                        {"User-Agent": "td-alerts"}):
            rows[t] = (t, op, hi, lo, cl)
        end = start
    now = time.time()
    return [rows[t] for t in sorted(rows) if t + secs <= now]


FEEDS = {"mexc": mexc_candles, "binance": binance_candles, "yahoo": yahoo_candles, "coinbase": coinbase_candles}


def cfd_open(t):
    """Was the CFD market open for this candle? Sunday 18:00 to Friday 17:00 New York time,
    closed 17:00-18:00 every day (the CME break)."""
    ny = dt.datetime.fromtimestamp(t, NEW_YORK)
    return not (ny.hour == 17 or (ny.weekday() == 4 and ny.hour >= 17) or ny.weekday() == 5 or
                (ny.weekday() == 6 and ny.hour < 18))


def candles(market, tf):
    """(closed candles, note) — the note says when the backup feed had to be used."""
    try:
        bars, note = FEEDS[market["source"]](market["id"], tf), ""
    except Exception as e:
        backup = market.get("fallback")
        if not backup:
            raise
        log(f"{market['name']} {tf}: {market['source']} failed ({str(e)[:80]}) — using {backup['source']}")
        bars, note = FEEDS[backup["source"]](backup["id"], tf), backup.get("note", "")
    if market.get("session") == "cfd":
        bars = [b for b in bars if cfd_open(b[0])]
    return bars, note


# ───────── TD Sequential (td_sequential.pine) ─────────
def td_signals(bars, lb=4, setup_len=9, cd_lb=2, cd_len=13, recycle=True):
    """[(index, kind, perfected, risk_level)] with kind in buy9 / sell9 / buy13 / sell13."""
    h, l, c = [b[2] for b in bars], [b[3] for b in bars], [b[4] for b in bars]
    true_hi = [max(h[i], c[i - 1]) if i else h[i] for i in range(len(bars))]
    true_lo = [min(l[i], c[i - 1]) if i else l[i] for i in range(len(bars))]
    qual = cd_len - 5
    out = []
    buy_set = sell_set = 0
    b = dict(on=False, cd=0, qual=None, cancel=None, ext=None, rng=None)
    s = dict(b)
    for i in range(len(bars)):
        buy_set = buy_set + 1 if i >= lb and c[i] < c[i - lb] else 0
        sell_set = sell_set + 1 if i >= lb and c[i] > c[i - lb] else 0
        buy9, sell9 = buy_set == setup_len, sell_set == setup_len
        w = slice(max(0, i - setup_len + 1), i + 1)
        setup_hi, setup_lo = max(true_hi[w]), min(true_lo[w])
        if buy9:
            m = min(l[i - 2], l[i - 3])
            out.append((i, "buy9", l[i] <= m or l[i - 1] <= m, None))
        if sell9:
            m = max(h[i - 2], h[i - 3])
            out.append((i, "sell9", h[i] >= m or h[i - 1] >= m, None))
        # buy countdown
        if buy9 and (not b["on"] or recycle):
            b.update(on=True, cd=0, qual=None, cancel=setup_hi, ext=None, rng=None)
        if b["on"] and (sell9 or c[i] > b["cancel"]):
            b["on"] = False
        if b["on"]:
            if b["ext"] is None or l[i] < b["ext"]:
                b["ext"], b["rng"] = l[i], true_hi[i] - true_lo[i]
            if i >= cd_lb and c[i] <= l[i - cd_lb]:
                if b["cd"] < cd_len - 1:
                    b["cd"] += 1
                    if b["cd"] == qual:
                        b["qual"] = c[i]
                elif b["qual"] is not None and l[i] <= b["qual"]:
                    out.append((i, "buy13", False, b["ext"] - b["rng"]))
                    b["on"] = False
        # sell countdown
        if sell9 and (not s["on"] or recycle):
            s.update(on=True, cd=0, qual=None, cancel=setup_lo, ext=None, rng=None)
        if s["on"] and (buy9 or c[i] < s["cancel"]):
            s["on"] = False
        if s["on"]:
            if s["ext"] is None or h[i] > s["ext"]:
                s["ext"], s["rng"] = h[i], true_hi[i] - true_lo[i]
            if i >= cd_lb and c[i] >= h[i - cd_lb]:
                if s["cd"] < cd_len - 1:
                    s["cd"] += 1
                    if s["cd"] == qual:
                        s["qual"] = c[i]
                elif s["qual"] is not None and h[i] >= s["qual"]:
                    out.append((i, "sell13", False, s["ext"] + s["rng"]))
                    s["on"] = False
    return out


# ───────── Alerts ─────────
def message(market, tf, bar, kind, perfected, risk, note=""):
    side = "BUY" if kind.startswith("buy") else "SELL"
    when = dt.datetime.utcfromtimestamp(bar[0]).strftime("%Y-%m-%d %H:%M UTC")
    if kind.endswith("9"):
        head = f"{'🟢' if side == 'BUY' else '🔴'} {side} 9 {'(perfected)' if perfected else '(not perfected)'}"
        extra = ""
    else:
        head = f"{'🟩' if side == 'BUY' else '🟥'} {side} 13 — countdown complete"
        extra = f"\nTD risk level {risk:.5g}"
    feed = f"\n({note})" if note else ""
    return f"{head}\n{market['name']} {tf.upper()} · close {bar[4]:.5g} · candle {when}{extra}{feed}"


def telegram(cfg, text):
    tg = cfg.get("telegram", {})
    token = os.environ.get("TELEGRAM_TOKEN") or tg.get("token", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id", "")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token.strip()}/sendMessage",
                                 data=json.dumps({"chat_id": str(chat).strip(), "text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
        return True
    except Exception as e:
        log(f"Telegram failed: {e}")
        return False


def scan(cfg, state, dry):
    for m in cfg["markets"]:
        for tf in cfg["timeframes"]:
            chart = f"{m['name']}|{tf}"
            try:
                bars, note = candles(m, tf)
            except Exception as e:
                log(f"{chart}: {e}")
                continue
            if not bars:
                continue
            sigs = td_signals(bars)
            if dry:
                log(f"{chart}: {len(bars)} closed candles, last {dt.datetime.utcfromtimestamp(bars[-1][0]):%Y-%m-%d %H:%M} UTC"
                    + (f" ({note})" if note else ""))
                for i, kind, perf, risk in sigs[-4:]:
                    print("    " + message(m, tf, bars[i], kind, perf, risk).replace("\n", " | "))
                continue
            seen = state.get(chart)
            if seen is None:              # first time: silent, start from the newest candle
                state[chart] = bars[-1][0]
                continue
            # send candle by candle; state only moves past a candle once all its signals went out
            for t in sorted({bars[i][0] for i, *_ in sigs if bars[i][0] > seen}):
                batch = [(i, kind, perf, risk) for i, kind, perf, risk in sigs if bars[i][0] == t]
                if not all(telegram(cfg, message(m, tf, bars[i], kind, perf, risk, note)) for i, kind, perf, risk in batch):
                    break
                state[chart] = t
                log(f"sent {chart}: " + ", ".join(k for _, k, _, _ in batch))


def main():
    dry, once = "--dry-run" in sys.argv, "--once" in sys.argv
    with open(CONFIG) as f:
        cfg = json.load(f)
    try:
        state = json.load(open(STATE))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    while True:
        scan(cfg, state, dry)
        if dry:
            return
        with open(STATE, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        if once:
            return
        time.sleep(900 - time.time() % 900 + 20)   # 20 seconds after the next 15-minute close


if __name__ == "__main__":
    main()
