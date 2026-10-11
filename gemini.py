"""Gemini Predictions: live odds + resolution (production public API) and
hedge orders (sandbox, authenticated)."""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

PROD = "https://api.gemini.com"
SANDBOX = "https://api.sandbox.gemini.com"
UTC = dt.timezone.utc


def _get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "freeif/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def event(ticker, base=PROD):
    d = _get(f"{base}/v1/prediction-markets/events/{ticker}")
    if isinstance(d, dict) and "data" in d and isinstance(d["data"], dict):
        d = d["data"]
    return d


# ------------------------------------------------------------ contract selection
def window_ticker(asset: str, min_left_s: int = 90):
    """5-minute UP contract whose window is live now. Tickers encode expiry in UTC:
    XRP05M2610112035 -> expires 2026-10-11 20:35 UTC."""
    now = dt.datetime.now(UTC)
    exp = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=5 - now.minute % 5)
    if (exp - now).total_seconds() < min_left_s:
        exp += dt.timedelta(minutes=5)
    return f"{asset}05M{exp:%y%m%d%H%M}", exp


def symbol(ticker):
    return f"GEMI-{ticker}-UP"


def price_of(c):
    """Market-implied P(YES) for one contract dict from the live book."""
    p = c.get("prices") or {}
    f = lambda v: float(v) if v not in (None, "") else None
    buy, sell = p.get("buy") or {}, p.get("sell") or {}
    # YES ask: what it costs to buy YES now. Selling NO at x is the same as buying YES at 1-x.
    ask = f(p.get("bestAsk")) or f(buy.get("yes")) or (1 - f(sell["no"]) if f(sell.get("no")) else None)
    # YES bid: what you get selling YES now. Buying NO at x is the same as selling YES at 1-x.
    bid = f(p.get("bestBid")) or f(sell.get("yes")) or (1 - f(buy["no"]) if f(buy.get("no")) else None)
    if ask is not None and bid is not None:
        return round((ask + bid) / 2, 4), {"bid": bid, "ask": ask, "basis": "mid"}
    if ask is not None:  # one-sided book: price the hedge at what we would actually pay
        return round(ask, 4), {"ask": ask, "basis": "ask (no bid)"}
    if bid is not None:
        return round(bid, 4), {"bid": bid, "basis": "bid (no ask)"}
    if f(p.get("lastTradePrice")) is not None:
        return f(p["lastTradePrice"]), {"basis": "last trade"}
    return None, {"basis": "empty book"}


def odds(ticker):
    """Market-implied P(UP) for a 5-minute contract from the live production book."""
    return price_of(event(ticker)["contracts"][0])


_MK = {"t": 0, "data": []}


def markets(limit=8):
    """Live Gemini markets for the promo marketplace: sports first, then crypto 5-minute.
    Every row is a real contract with a live price; cached for 20 seconds."""
    if time.time() - _MK["t"] < 20 and _MK["data"]:
        return _MK["data"]
    d = _get(f"{PROD}/v1/prediction-markets/events?status=active&limit=200")
    evs = d if isinstance(d, list) else (d.get("data") or d.get("events") or [])
    rows = []
    for e in evs:
        cat = str(e.get("category", "")).lower()
        kind = "sports" if "sport" in cat else ("crypto5m" if "05M" in str(e.get("ticker", "")) else None)
        if not kind:
            continue
        for c in (e.get("contracts") or [])[:2]:
            pr, book = price_of(c)
            if pr is None or not (0.03 < pr < 0.97):
                continue
            rows.append({"kind": kind, "event": e.get("title"), "ticker": e.get("ticker"),
                         "contract": c.get("label") or c.get("abbreviatedName") or c.get("ticker"),
                         "p": pr, "basis": book.get("basis"), "expiry": e.get("expiryDate")})
            break
    rows.sort(key=lambda r: (r["kind"] != "sports", r["expiry"] or ""))
    seen, out = set(), []
    for r in rows:  # one row per event title family
        k = (r["kind"], (r["event"] or "")[:18])
        if k in seen:
            continue
        seen.add(k); out.append(r)
        if len(out) >= limit:
            break
    _MK.update(t=time.time(), data=out)
    return out


# ------------------------------------------------------------ resolution
_RESULT_KEYS = ("resolution", "outcome", "result", "resolvedOutcome", "winningOutcome",
                "settlementOutcome", "settledOutcome", "resolvedSide")


def _as_bool(v):
    v = str(v).strip().lower()
    if v in ("yes", "up", "true", "1", "y"):
        return True
    if v in ("no", "down", "false", "0", "n"):
        return False
    return None


def resolve(ticker, asset, expiry: dt.datetime):
    """Return (outcome, source, raw). outcome is True/False, or None if not yet known.
    1) Gemini's own resolution on the contract (preferred).
    2) Fallback: Gemini BTCUSD/XRPUSD 1-minute candles, window open vs window close."""
    raw = {}
    try:
        e = event(ticker)
        c = (e.get("contracts") or [{}])[0]
        raw = {k: c.get(k) for k in ("status", "marketState", "prices") if k in c}
        raw["eventStatus"] = e.get("status")
        for src in (c, e):
            for k in _RESULT_KEYS:
                if k in src and src[k] not in (None, ""):
                    val = src[k]
                    if isinstance(val, dict):
                        val = val.get("outcome") or val.get("value") or val.get("side")
                    b = _as_bool(val)
                    if b is not None:
                        return b, f"gemini:{ticker}:{k}", raw
        status = str(c.get("status", "")).lower()
        last = (c.get("prices") or {}).get("lastTradePrice")
        if status in ("settled", "resolved") and last is not None and float(last) in (0.0, 1.0):
            return float(last) == 1.0, f"gemini:{ticker}:settledPrice", raw
    except Exception as ex:  # expired events may 404; fall through to candles
        raw["error"] = str(ex)[:200]

    now = dt.datetime.now(UTC)
    if now < expiry + dt.timedelta(seconds=75):  # wait for the closing candle to finalize
        return None, "waiting", raw
    try:
        candles = _get(f"{PROD}/v2/candles/{asset.lower()}usd/1m")  # [[ms, o, h, l, c, v], ...]
        by_t = {int(k[0]) // 1000: k for k in candles}
        start = int((expiry - dt.timedelta(minutes=5)).timestamp())
        last_min = int((expiry - dt.timedelta(minutes=1)).timestamp())
        if start in by_t and last_min in by_t:
            p0, p1 = float(by_t[start][1]), float(by_t[last_min][4])
            raw["candles"] = {"open": p0, "close": p1}
            return p1 >= p0, f"gemini-candles:{asset}USD {p0}->{p1}", raw
    except Exception as ex:
        raw["candle_error"] = str(ex)[:200]
    return None, "waiting", raw


# ------------------------------------------------------------ sandbox hedge (authenticated)
def _private(path, payload=None):
    key, secret = os.environ.get("GEMINI_KEY"), os.environ.get("GEMINI_SECRET")
    if not key or not secret:
        return {"skipped": "GEMINI_KEY / GEMINI_SECRET not set"}
    body = {"request": path, "nonce": str(int(time.time() * 1000))}
    body.update(payload or {})
    b64 = base64.b64encode(json.dumps(body).encode())
    sig = hmac.new(secret.encode(), b64, hashlib.sha384).hexdigest()
    req = urllib.request.Request(SANDBOX + path, data=b"", method="POST", headers={
        "Content-Type": "text/plain", "Content-Length": "0", "Cache-Control": "no-cache",
        "X-GEMINI-APIKEY": key, "X-GEMINI-PAYLOAD": b64.decode(), "X-GEMINI-SIGNATURE": sig})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"HTTP_ERROR": e.code, "body": e.read().decode()[:500]}


def hedge(ticker, quantity, price):
    """Buy YES on the same contract the promo is tied to. If the promo pays out,
    the hedge pays the merchant $1 per contract. Sandbox has no external makers,
    so the order may rest unfilled; we report exactly what the exchange returns."""
    _private("/v1/prediction-markets/terms/accept")
    px = min(0.99, max(0.01, round(price, 2)))
    return _private("/v1/prediction-markets/order", {
        "symbol": symbol(ticker), "orderType": "limit", "side": "buy", "outcome": "yes",
        "quantity": str(int(quantity)), "price": f"{px:.2f}", "timeInForce": "good-til-cancel"})
