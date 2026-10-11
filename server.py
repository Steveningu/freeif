"""FreeIf server.

Run:  uvicorn server:app --port 8000      then open http://localhost:8000
"""
import asyncio
import datetime as dt
import json
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import attester
import gemini
import xrpl_ops as X

UTC = dt.timezone.utc
CANCEL_GRACE = dt.timedelta(minutes=8)   # attester must release before this; after it, NO-branch escrows return to merchant
CHECKOUT_CUTOFF_S = 20                   # no new promo purchases in the last seconds of the window

app = FastAPI(title="FreeIf")
W = X.load_wallets()
ISSUER = W["issuer"].address
MERCHANT = W["merchant"]
ATTESTER = W["attester"]

S = {"campaign": None, "customers": [], "feed": [], "simulate": [], "odds": {}, "merchant_balance": None}


def reset_customers():
    S["customers"] = [{"idx": i, "address": c.address, "paid": False, "status": "idle",
                       "pay_tx": None, "escrow_tx": None, "escrow_seq": None, "final_tx": None,
                       "balance": None} for i, c in enumerate(W["customers"])]


reset_customers()
STATE_FILE = Path(__file__).parent / "state.json"
try:  # survive restarts: escrow sequences and promo state are needed to settle
    S.update(json.load(open(STATE_FILE)))
except (FileNotFoundError, ValueError):
    pass


def save():
    try:
        json.dump(S, open(STATE_FILE, "w"), default=str)
    except Exception:
        pass


def log(label, code=None, h=None, note=None):
    if S["feed"] and S["feed"][0]["label"] == label and code is None:
        S["feed"][0]["t"] = time.strftime("%H:%M:%S")  # collapse repeated errors
        return
    S["feed"].insert(0, {"t": time.strftime("%H:%M:%S"), "label": label, "code": code,
                         "link": X.tx_link(h) if h else None, "note": note})
    del S["feed"][60:]


def now():
    return dt.datetime.now(UTC)


# ------------------------------------------------------------------ API
class CampaignIn(BaseModel):
    store: str = "Houston Home Goods"
    asset: str = "XRP"
    price: float = 100
    refund_pct: float = 50
    units: int = 5


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/shop")
def shop():
    return FileResponse(Path(__file__).parent / "static" / "shop.html")


@app.get("/api/markets")
async def markets():
    try:
        return {"markets": await asyncio.to_thread(gemini.markets)}
    except Exception as ex:
        return {"markets": [], "error": str(ex)[:120]}


@app.post("/api/buy")
async def buy():
    """Consumer checkout from the storefront: the next test customer wallet pays."""
    for u in S["customers"]:
        if not u["paid"]:
            out = await _checkout(u["idx"])
            return {"customer": out, "refund": S["campaign"]["refund"], "price": S["campaign"]["price"]}
    raise HTTPException(400, "all demo customers have checked out")


@app.get("/api/state")
def state():
    c = S["campaign"]
    out = dict(S)
    if c:
        out["seconds_left"] = max(0, int((dt.datetime.fromisoformat(c["expiry"]) - now()).total_seconds()))
        out["liability"] = round(sum(c["refund"] for u in S["customers"] if u["paid"]), 2)
        out["max_liability"] = round(c.get("units", 5) * c["refund"], 2)
    out["merchant"] = MERCHANT.address
    out["attester"] = ATTESTER.address
    return out


@app.post("/api/campaign")
async def create_campaign(body: CampaignIn):
    c = S["campaign"]
    if c and c["status"] in ("open", "awaiting"):
        raise HTTPException(400, "a promo is already running")
    asset = body.asset.upper()
    ticker, expiry = gemini.window_ticker(asset)
    p, book = await asyncio.to_thread(gemini.odds, ticker)
    cid = uuid.uuid4().hex[:8]
    refund = round(body.price * body.refund_pct / 100, 2)
    S["campaign"] = {
        "id": cid, "store": body.store, "units": max(1, min(body.units, len(W["customers"]))), "asset": asset, "ticker": ticker, "symbol": gemini.symbol(ticker),
        "expiry": expiry.isoformat(), "cancel_after": (expiry + CANCEL_GRACE).isoformat(),
        "price": body.price, "refund": refund, "condition": attester.new_condition(cid),
        "odds_at_start": p, "status": "open", "outcome": None, "outcome_source": None,
        "oracle_tx": None, "hedge": None, "doc_id": int(time.time()) % 100000,
    }
    S["odds"] = {"ticker": ticker, "p": p, **book, "updated": time.strftime("%H:%M:%S")} if p is not None else {}
    S["simulate"] = []
    S["odds_hist"] = [[int(time.time()), p]] if p is not None else []
    reset_customers()
    log(f"Promo created: refund {body.refund_pct:.0f}% if {asset} is UP at {expiry:%H:%M} UTC "
        f"(Gemini {ticker}, P(up)={p})")
    asyncio.create_task(preview())
    return S["campaign"]


async def preview():
    """simulate (XLS-69): dry-run every refund escrow on the live ledger before anyone pays."""
    c = S["campaign"]
    cancel_after = dt.datetime.fromisoformat(c["cancel_after"])
    results = []
    for u in S["customers"]:
        tx = X.escrow_tx(MERCHANT.address, u["address"], ISSUER, c["refund"], c["condition"], cancel_after)
        try:
            r = await asyncio.to_thread(X.simulate, tx)
        except Exception as ex:
            r = {"engine_result": f"error: {ex}"[:80], "creates": []}
        results.append({"idx": u["idx"], **r})
        S["simulate"] = list(results)
    ok = sum(1 for r in results if r.get("engine_result") == "tesSUCCESS")
    log(f"simulate: {ok}/{len(results)} refund escrows would succeed, "
        f"{ok * c['refund']:.2f} RLUSD would lock (nothing submitted)")


async def _checkout(i):
    c, u = S["campaign"], S["customers"][i]
    if not c or c["status"] != "open":
        raise HTTPException(400, "no open promo")
    if u["paid"]:
        return u
    if sum(1 for x in S["customers"] if x["paid"]) >= c.get("units", 99):
        raise HTTPException(400, "unit cap reached")
    left = (dt.datetime.fromisoformat(c["expiry"]) - now()).total_seconds()
    if left < CHECKOUT_CUTOFF_S:
        raise HTTPException(400, "promo window closing")
    u["status"] = "paying"
    code, h, _ = await asyncio.to_thread(X.pay, W["customers"][i], MERCHANT.address, ISSUER, c["price"])
    log(f"Customer {i+1} pays {c['price']} RLUSD", code, h)
    if code != "tesSUCCESS":
        u["status"] = "failed"
        return u
    u["paid"], u["pay_tx"], u["status"] = True, X.tx_link(h), "locking"
    code, h, seq = await asyncio.to_thread(X.lock_refund, MERCHANT, u["address"], ISSUER, c["refund"],
                                           c["condition"], dt.datetime.fromisoformat(c["cancel_after"]))
    log(f"Refund {c['refund']} RLUSD locked in escrow for customer {i+1}", code, h)
    u["escrow_tx"], u["escrow_seq"] = X.tx_link(h), seq
    u["status"] = "locked" if code == "tesSUCCESS" else "failed"
    return u


@app.post("/api/checkout/{i}")
async def checkout(i: int):
    return await _checkout(i)


@app.post("/api/checkout_all")
async def checkout_all():
    async def run():
        for u in S["customers"]:
            if not u["paid"]:
                try:
                    await _checkout(u["idx"])
                except HTTPException as e:
                    log(f"checkout stopped: {e.detail}")
                    return
    asyncio.create_task(run())
    return {"started": True}


@app.post("/api/hedge")
async def hedge():
    c = S["campaign"]
    if not c:
        raise HTTPException(400, "no promo")
    qty = sum(c["refund"] for u in S["customers"] if u["status"] == "locked")
    if qty <= 0:
        raise HTTPException(400, "nothing locked yet")
    p = S["odds"].get("p") or c["odds_at_start"] or 0.5
    r = await asyncio.to_thread(gemini.hedge, c["ticker"], qty, p)
    c["hedge"] = {"quantity": int(qty), "price": round(p, 2), "response": r}
    verb = "Hedge plan (not submitted, no sandbox key)" if "skipped" in r else "Gemini sandbox hedge order"
    log(f"{verb}: buy {int(qty)} YES {c['symbol']} @ {p:.2f}", note=None if "skipped" in r else str(r)[:160])
    return c["hedge"]


@app.post("/api/reset")
def reset():
    c = S["campaign"]
    if c and c["status"] in ("open", "awaiting"):
        raise HTTPException(400, "promo still running")
    S["campaign"], S["simulate"] = None, []
    reset_customers()
    return {"ok": True}


# ------------------------------------------------------------------ background: odds, balances, attester
async def ticker_loop():
    n = 0
    while True:
        n += 1
        c = S["campaign"]
        try:
            if c and c["status"] == "open":
                p, book = await asyncio.to_thread(gemini.odds, c["ticker"])
                if p is not None:
                    S["odds"] = {"ticker": c["ticker"], "p": p, **book, "updated": time.strftime("%H:%M:%S")}
                    S.setdefault("odds_hist", []).append([int(time.time()), p])
                    del S["odds_hist"][:-300]
                elif S["odds"].get("ticker") == c["ticker"]:
                    S["odds"]["basis"] = f"last seen {S['odds'].get('updated')}, book now empty"
            if n % 3 == 0:
                S["merchant_balance"] = await asyncio.to_thread(X.balance, MERCHANT.address, ISSUER)
                for u in S["customers"]:
                    u["balance"] = await asyncio.to_thread(X.balance, u["address"], ISSUER)
            if c:
                await attest_step(c)
        except Exception as ex:
            log(f"loop error: {ex}"[:200])
        save()
        await asyncio.sleep(2)


async def attest_step(c):
    expiry = dt.datetime.fromisoformat(c["expiry"])
    if c["status"] == "open" and now() >= expiry:
        c["status"] = "awaiting"
        log(f"Window closed. Attester waiting for Gemini to resolve {c['ticker']}")
    if c["status"] == "awaiting":
        outcome, source, raw = await asyncio.to_thread(gemini.resolve, c["ticker"], c["asset"], expiry)
        c["resolution_raw"] = raw
        if outcome is None:
            return
        c["outcome"], c["outcome_source"] = outcome, source
        try:  # the oracle record is evidence; it must never block refunds
            code, h, _ = await asyncio.to_thread(X.publish_outcome, ATTESTER, c["doc_id"], c["asset"], outcome, source)
            c["oracle_tx"] = X.tx_link(h)
            log(f"Attester publishes outcome to XRPL oracle: {c['asset']} {'UP (YES)' if outcome else 'DOWN (NO)'} [{source}]", code, h)
        except Exception as ex:
            log(f"Oracle publish failed ({str(ex)[:80]}); settling escrows anyway. Outcome: {'UP' if outcome else 'DOWN'} [{source}]")
        c["status"] = "resolved_yes" if outcome else "resolved_no"
        if outcome:
            ful = attester.fulfillment(c["id"])
            for u in S["customers"]:
                if u["status"] == "locked":
                    code, h, _ = await asyncio.to_thread(X.release, ATTESTER, MERCHANT.address,
                                                         u["escrow_seq"], c["condition"], ful)
                    u["final_tx"], u["status"] = X.tx_link(h), ("refunded" if code == "tesSUCCESS" else "failed")
                    log(f"Refund released to customer {u['idx']+1}", code, h)
            c["status"] = "settled"
    if c["status"] == "resolved_no" and now() >= dt.datetime.fromisoformat(c["cancel_after"]) + dt.timedelta(seconds=10):
        for u in S["customers"]:
            if u["status"] == "locked":
                code, h, _ = await asyncio.to_thread(X.cancel, MERCHANT, MERCHANT.address, u["escrow_seq"])
                u["final_tx"], u["status"] = X.tx_link(h), ("returned" if code == "tesSUCCESS" else "failed")
                log(f"Escrow expired, refund returned to merchant (customer {u['idx']+1})", code, h)
        c["status"] = "settled"


@app.on_event("startup")
async def start():
    asyncio.create_task(ticker_loop())
    log("FreeIf online. Merchant " + MERCHANT.address)
