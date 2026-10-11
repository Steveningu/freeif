"""XRPL operations for FreeIf.

Every function here does one real ledger action on XRPL Testnet:
  - Payment          customer pays the merchant at checkout
  - EscrowCreate     merchant locks the customer's refund (token escrow + crypto-condition)
  - simulate         dry run of the escrow batch before the promo starts
  - OracleSet        attester publishes the Gemini outcome on-ledger
  - EscrowFinish     attester releases refunds (YES)
  - EscrowCancel     refunds return to the merchant after expiry (NO)
"""
import datetime as dt
import json
import threading
import urllib.request

from xrpl.clients import JsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import AccountLines
from xrpl.models.transactions import (EscrowCancel, EscrowCreate, EscrowFinish,
                                      OracleSet, Payment)
from xrpl.transaction import autofill, submit_and_wait
from xrpl.utils import datetime_to_ripple_time, str_to_hex
from xrpl.wallet import Wallet

try:
    from xrpl.models.transactions.oracle_set import PriceData
except ImportError:  # older layouts
    from xrpl.models import PriceData

TESTNET = "https://s.altnet.rippletest.net:51234"
EXPLORER = "https://testnet.xrpl.org"
CURRENCY = "USD"  # test dollar issued by our own issuer, stands in for RLUSD on testnet

client = JsonRpcClient(TESTNET)
_lock = threading.Lock()  # one submission at a time keeps account sequences clean


def load_wallets(path="wallets.json"):
    raw = json.load(open(path))
    w = {k: Wallet.from_seed(raw[k]) for k in ("issuer", "merchant", "attester")}
    w["customers"] = [Wallet.from_seed(s) for s in raw["customers"]]
    return w


def usd(issuer_addr, value):
    return IssuedCurrencyAmount(currency=CURRENCY, issuer=issuer_addr, value=str(value))


def tx_link(h):
    return f"{EXPLORER}/transactions/{h}"


def submit(tx, wallet):
    """Submit, wait for validation, return (result_code, hash, full_result)."""
    with _lock:
        r = submit_and_wait(tx, client, wallet).result
    return r["meta"]["TransactionResult"], r["hash"], r


def balance(addr, issuer_addr):
    lines = client.request(AccountLines(account=addr, peer=issuer_addr)).result.get("lines", [])
    for ln in lines:
        if ln["currency"] == CURRENCY:
            return float(ln["balance"])
    return 0.0


def ripple_time(t: dt.datetime) -> int:
    return datetime_to_ripple_time(t.astimezone(dt.timezone.utc))


# ---------------------------------------------------------------- checkout
def pay(customer: Wallet, merchant_addr, issuer_addr, amount):
    tx = Payment(account=customer.address, destination=merchant_addr, amount=usd(issuer_addr, amount))
    return submit(tx, customer)


def escrow_tx(merchant_addr, customer_addr, issuer_addr, amount, condition, cancel_after: dt.datetime):
    return EscrowCreate(account=merchant_addr, destination=customer_addr,
                        amount=usd(issuer_addr, amount), condition=condition,
                        cancel_after=ripple_time(cancel_after))


def lock_refund(merchant: Wallet, customer_addr, issuer_addr, amount, condition, cancel_after):
    code, h, r = submit(escrow_tx(merchant.address, customer_addr, issuer_addr, amount, condition, cancel_after), merchant)
    seq = (r.get("tx_json") or r).get("Sequence")
    return code, h, seq


# ---------------------------------------------------------------- simulate (XLS-69)
def simulate(tx):
    """Dry-run a transaction on the live ledger. Nothing is submitted."""
    filled = autofill(tx, client)
    tx_json = filled.to_xrpl()
    for k in ("SigningPubKey", "TxnSignature"):
        tx_json.pop(k, None)
    body = json.dumps({"method": "simulate", "params": [{"tx_json": tx_json}]}).encode()
    req = urllib.request.Request(TESTNET, body, {"Content-Type": "application/json"})
    res = json.load(urllib.request.urlopen(req, timeout=20)).get("result", {})
    meta = res.get("meta") or {}
    created = [n["CreatedNode"]["LedgerEntryType"] for n in meta.get("AffectedNodes", []) if "CreatedNode" in n]
    return {"engine_result": res.get("engine_result"), "applied": res.get("applied"), "creates": created}


# ---------------------------------------------------------------- attester
def publish_outcome(attester: Wallet, doc_id: int, asset: str, outcome_yes: bool, source: str):
    """Write the Gemini result to an on-ledger price oracle (XLS-47).
    PriceData: base=<asset>, quote 'YES' (resolved UP) or 'NAY' (resolved DOWN), price 1.0.
    XRPL currency codes must be exactly 3 characters (or 40 hex), so 'YES' rather than 'UP'."""
    tx = OracleSet(account=attester.address, oracle_document_id=doc_id,
                   provider=str_to_hex("gemini-predictions"), uri=str_to_hex(source[:200]),
                   asset_class=str_to_hex("event"),
                   last_update_time=int(dt.datetime.now(dt.timezone.utc).timestamp()),
                   price_data_series=[PriceData(base_asset=asset, quote_asset="YES" if outcome_yes else "NAY",
                                                asset_price=10, scale=1)])
    return submit(tx, attester)


def release(attester: Wallet, merchant_addr, seq, condition, fulfillment):
    tx = EscrowFinish(account=attester.address, owner=merchant_addr, offer_sequence=seq,
                      condition=condition, fulfillment=fulfillment)
    return submit(tx, attester)


def cancel(any_wallet: Wallet, merchant_addr, seq):
    return submit(EscrowCancel(account=any_wallet.address, owner=merchant_addr, offer_sequence=seq), any_wallet)
